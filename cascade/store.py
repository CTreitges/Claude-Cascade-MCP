"""SQLite persistence for tasks, iterations, logs."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import aiosqlite

TaskStatus = Literal[
    "pending", "running", "interrupted", "done", "failed", "cancelled"
]
LogLevel = Literal["debug", "info", "warn", "error"]
Source = Literal["mcp", "telegram", "cli"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    task_text       TEXT NOT NULL,
    repo_path       TEXT,
    workspace_path  TEXT,
    status          TEXT NOT NULL,
    iteration       INTEGER NOT NULL DEFAULT 0,
    implementer_model TEXT,
    implementer_tools TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    completed_at    REAL,
    result_summary  TEXT,
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS iterations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    n                   INTEGER NOT NULL,
    implementer_output  TEXT,
    reviewer_pass       INTEGER,
    reviewer_feedback   TEXT,
    diff_excerpt        TEXT,
    created_at          REAL NOT NULL,
    UNIQUE(task_id, n)
);
CREATE INDEX IF NOT EXISTS idx_iter_task ON iterations(task_id);

CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    level     TEXT NOT NULL,
    ts        REAL NOT NULL,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_task_ts ON logs(task_id, ts);

CREATE TABLE IF NOT EXISTS skills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    description   TEXT,
    task_template TEXT NOT NULL,
    rationale     TEXT,
    source_task_ids TEXT,
    usage_count   INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_used_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);

CREATE TABLE IF NOT EXISTS skill_suggestions (
    task_id     TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    suggestion_json TEXT NOT NULL,
    chat_id     INTEGER,
    created_at  REAL NOT NULL,
    decided_at  REAL,
    decision    TEXT  -- 'accepted' | 'rejected' | NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id     INTEGER PRIMARY KEY,
    repo_path   TEXT,
    last_task_id TEXT,
    planner_model TEXT,
    implementer_model TEXT,
    reviewer_model TEXT,
    chat_model TEXT,
    planner_effort TEXT,
    reviewer_effort TEXT,
    triage_effort TEXT,
    implementer_effort TEXT,
    planner_temperature REAL,
    implementer_temperature REAL,
    reviewer_temperature REAL,
    chat_temperature REAL,
    replan_max INTEGER,
    max_iterations INTEGER,
    replan_after_failures INTEGER,
    triage_enabled INTEGER,
    auto_skill_suggest INTEGER,
    context7_enabled INTEGER,
    websearch_enabled INTEGER,
    lang TEXT,
    auto_decompose INTEGER,
    max_subtasks INTEGER,
    multiplan_enabled INTEGER,
    updated_at  REAL NOT NULL
);

-- Persistent chat history per chat_id. Stores text + optional file content
-- (up to 30KB inline) so the bot can reference uploaded files even after
-- restarts. Retention is unlimited by default — only `/forget` removes
-- entries. The Hot-Layer (last 30 msgs) is fed verbatim into the triage
-- prompt; older messages are recalled via FTS5 search.
CREATE TABLE IF NOT EXISTS chat_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL,
    role                TEXT NOT NULL,
    text                TEXT NOT NULL,
    ts                  REAL NOT NULL,
    file_path           TEXT,
    file_content        TEXT,
    file_classification TEXT,
    summarized          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_ts
    ON chat_messages(chat_id, ts);

-- Compressed Sonnet-summaries of older chat windows. Built lazily by a
-- background task once a window of ~50 messages crosses 7 days of age.
-- Used to give triage a long-horizon recall without dumping thousands
-- of raw lines into the system prompt.
CREATE TABLE IF NOT EXISTS chat_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    period_from REAL NOT NULL,
    period_to   REAL NOT NULL,
    summary     TEXT NOT NULL,
    msg_count   INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_summaries_chat_ts
    ON chat_summaries(chat_id, period_to);

-- Recently uploaded files (24h sliding window). Independent of
-- chat_messages so the smart-document handler can answer "did I receive
-- a JSON earlier?" without scanning the whole history. `handled=1`
-- means a direct_action / cascade has already done something with it
-- (placed it, used it as input, etc.).
CREATE TABLE IF NOT EXISTS pending_attachments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL,
    file_name          TEXT NOT NULL,
    file_path          TEXT NOT NULL,
    classification     TEXT,
    received_at        REAL NOT NULL,
    handled            INTEGER NOT NULL DEFAULT 0,
    handled_by_task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_attachments_chat_ts
    ON pending_attachments(chat_id, received_at);

-- FTS5 mirror is created in a separate optional script (see _SCHEMA_FTS)
-- because some SQLite builds ship without FTS5; in that case the
-- ChatMemory layer falls back to LIKE queries.

-- Human-in-the-loop questions: an agent in the cascade can call
-- `await ask_user(...)` to pause and request clarification from the
-- user via Telegram. The user's next free-form message is captured
-- as the answer, the cascade resumes, no new task is started.
CREATE TABLE IF NOT EXISTS chat_questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    task_id       TEXT,
    question      TEXT NOT NULL,
    asked_at      REAL NOT NULL,
    answer        TEXT,
    answered_at   REAL,
    expired_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_chat_questions_pending
    ON chat_questions(chat_id, answered_at, expired_at);

-- Per-chat persistent facts the user has established (project paths,
-- service-account locations, GitHub username, …). Survives restarts and
-- gets injected into every triage / cascade prompt as ground-truth context.
CREATE TABLE IF NOT EXISTS user_facts (
    chat_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""

# FTS5 setup — tried separately because not every SQLite build has it.
# Failure here is not fatal; ChatMemory falls back to LIKE search.
_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts
    USING fts5(text, file_content, content='chat_messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS chat_messages_ai AFTER INSERT ON chat_messages BEGIN
    INSERT INTO chat_messages_fts(rowid, text, file_content)
    VALUES (new.id, new.text, COALESCE(new.file_content, ''));
END;
CREATE TRIGGER IF NOT EXISTS chat_messages_ad AFTER DELETE ON chat_messages BEGIN
    INSERT INTO chat_messages_fts(chat_messages_fts, rowid, text, file_content)
    VALUES('delete', old.id, old.text, COALESCE(old.file_content, ''));
END;
CREATE TRIGGER IF NOT EXISTS chat_messages_au AFTER UPDATE ON chat_messages BEGIN
    INSERT INTO chat_messages_fts(chat_messages_fts, rowid, text, file_content)
    VALUES('delete', old.id, old.text, COALESCE(old.file_content, ''));
    INSERT INTO chat_messages_fts(rowid, text, file_content)
    VALUES (new.id, new.text, COALESCE(new.file_content, ''));
END;
"""


# Best-effort additive migration for existing DBs.
_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN planner_model TEXT",
    "ALTER TABLE sessions ADD COLUMN implementer_model TEXT",
    "ALTER TABLE sessions ADD COLUMN reviewer_model TEXT",
    "ALTER TABLE sessions ADD COLUMN planner_effort TEXT",
    "ALTER TABLE sessions ADD COLUMN reviewer_effort TEXT",
    "ALTER TABLE sessions ADD COLUMN triage_effort TEXT",
    "ALTER TABLE sessions ADD COLUMN replan_max INTEGER",
    "ALTER TABLE sessions ADD COLUMN chat_model TEXT",
    "ALTER TABLE sessions ADD COLUMN implementer_effort TEXT",
    "ALTER TABLE sessions ADD COLUMN planner_temperature REAL",
    "ALTER TABLE sessions ADD COLUMN implementer_temperature REAL",
    "ALTER TABLE sessions ADD COLUMN reviewer_temperature REAL",
    "ALTER TABLE sessions ADD COLUMN chat_temperature REAL",
    "ALTER TABLE sessions ADD COLUMN max_iterations INTEGER",
    "ALTER TABLE sessions ADD COLUMN replan_after_failures INTEGER",
    "ALTER TABLE sessions ADD COLUMN triage_enabled INTEGER",
    "ALTER TABLE sessions ADD COLUMN auto_skill_suggest INTEGER",
    "ALTER TABLE sessions ADD COLUMN context7_enabled INTEGER",
    "ALTER TABLE sessions ADD COLUMN websearch_enabled INTEGER",
    "ALTER TABLE sessions ADD COLUMN lang TEXT",
    "ALTER TABLE sessions ADD COLUMN auto_decompose INTEGER",
    "ALTER TABLE sessions ADD COLUMN max_subtasks INTEGER",
    "ALTER TABLE sessions ADD COLUMN multiplan_enabled INTEGER",
    # Chat-Memory v2 — file content + classification on chat_messages
    "ALTER TABLE chat_messages ADD COLUMN file_path TEXT",
    "ALTER TABLE chat_messages ADD COLUMN file_content TEXT",
    "ALTER TABLE chat_messages ADD COLUMN file_classification TEXT",
    "ALTER TABLE chat_messages ADD COLUMN summarized INTEGER NOT NULL DEFAULT 0",
]


@dataclass
class Task:
    id: str
    source: Source
    task_text: str
    repo_path: str | None
    workspace_path: str | None
    status: TaskStatus
    iteration: int
    implementer_model: str | None
    implementer_tools: str | None
    created_at: float
    updated_at: float
    completed_at: float | None
    result_summary: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Task":
        return cls(
            id=row["id"],
            source=row["source"],
            task_text=row["task_text"],
            repo_path=row["repo_path"],
            workspace_path=row["workspace_path"],
            status=row["status"],
            iteration=row["iteration"],
            implementer_model=row["implementer_model"],
            implementer_tools=row["implementer_tools"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            result_summary=row["result_summary"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        )


@dataclass
class Iteration:
    n: int
    implementer_output: str | None
    reviewer_pass: bool | None
    reviewer_feedback: str | None
    diff_excerpt: str | None
    created_at: float


@dataclass
class LogEntry:
    level: LogLevel
    ts: float
    message: str


class Store:
    """Async wrapper around aiosqlite. Use `await Store.open(path)` then `await close()`."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass  # column already exists
        # FTS5 is optional — some SQLite builds ship without it.
        try:
            await conn.executescript(_SCHEMA_FTS)
            # Backfill FTS index for any rows inserted before the migration.
            await conn.execute(
                """INSERT INTO chat_messages_fts(rowid, text, file_content)
                   SELECT m.id, m.text, COALESCE(m.file_content, '')
                   FROM chat_messages m
                   LEFT JOIN chat_messages_fts f ON f.rowid = m.id
                   WHERE f.rowid IS NULL""",
            )
        except Exception:
            pass  # FTS5 unavailable — ChatMemory will fall back to LIKE
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    # ---------- tasks ----------

    async def create_task(
        self,
        *,
        source: Source,
        task_text: str,
        repo_path: str | None = None,
        workspace_path: str | None = None,
        implementer_model: str | None = None,
        implementer_tools: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        tid = uuid.uuid4().hex[:12]
        now = time.time()
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO tasks (id, source, task_text, repo_path, workspace_path,
                                      status, iteration, implementer_model, implementer_tools,
                                      created_at, updated_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    source,
                    task_text,
                    repo_path,
                    workspace_path,
                    implementer_model,
                    implementer_tools,
                    now,
                    now,
                    json.dumps(metadata or {}),
                ),
            )
        return tid

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        iteration: int | None = None,
        workspace_path: str | None = None,
        result_summary: str | None = None,
        completed: bool = False,
    ) -> None:
        sets: list[str] = ["updated_at = ?"]
        vals: list[Any] = [time.time()]
        if status is not None:
            sets.append("status = ?")
            vals.append(status)
        if iteration is not None:
            sets.append("iteration = ?")
            vals.append(iteration)
        if workspace_path is not None:
            sets.append("workspace_path = ?")
            vals.append(workspace_path)
        if result_summary is not None:
            sets.append("result_summary = ?")
            vals.append(result_summary)
        if completed:
            sets.append("completed_at = ?")
            vals.append(time.time())
        vals.append(task_id)
        async with self._tx() as c:
            await c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)

    async def get_task(self, task_id: str) -> Task | None:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return Task.from_row(row) if row else None

    async def list_tasks(self, limit: int = 10, status: TaskStatus | None = None) -> list[Task]:
        if status is None:
            q = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            args: tuple = (limit,)
        else:
            q = "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            args = (status, limit)
        async with self._conn.execute(q, args) as cur:
            rows = await cur.fetchall()
        return [Task.from_row(r) for r in rows]

    async def latest_task(self) -> Task | None:
        tasks = await self.list_tasks(limit=1)
        return tasks[0] if tasks else None

    async def mark_running_as_interrupted(self) -> list[str]:
        """Used at bot startup: any leftover 'running' is bot-crashed → 'interrupted'."""
        async with self._conn.execute(
            "SELECT id FROM tasks WHERE status = 'running'"
        ) as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        if ids:
            async with self._tx() as c:
                await c.executemany(
                    "UPDATE tasks SET status='interrupted', updated_at=? WHERE id=?",
                    [(time.time(), tid) for tid in ids],
                )
        return ids

    # ---------- iterations ----------

    async def record_iteration(
        self,
        task_id: str,
        n: int,
        *,
        implementer_output: str | None = None,
        reviewer_pass: bool | None = None,
        reviewer_feedback: str | None = None,
        diff_excerpt: str | None = None,
    ) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO iterations
                     (task_id, n, implementer_output, reviewer_pass,
                      reviewer_feedback, diff_excerpt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task_id, n) DO UPDATE SET
                     implementer_output = excluded.implementer_output,
                     reviewer_pass      = excluded.reviewer_pass,
                     reviewer_feedback  = excluded.reviewer_feedback,
                     diff_excerpt       = excluded.diff_excerpt
                """,
                (
                    task_id,
                    n,
                    implementer_output,
                    int(reviewer_pass) if reviewer_pass is not None else None,
                    reviewer_feedback,
                    diff_excerpt,
                    time.time(),
                ),
            )

    async def list_iterations(self, task_id: str) -> list[Iteration]:
        async with self._conn.execute(
            "SELECT * FROM iterations WHERE task_id = ? ORDER BY n ASC",
            (task_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Iteration(
                n=r["n"],
                implementer_output=r["implementer_output"],
                reviewer_pass=bool(r["reviewer_pass"]) if r["reviewer_pass"] is not None else None,
                reviewer_feedback=r["reviewer_feedback"],
                diff_excerpt=r["diff_excerpt"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ---------- logs ----------

    async def log(self, task_id: str, level: LogLevel, message: str) -> None:
        async with self._tx() as c:
            await c.execute(
                "INSERT INTO logs (task_id, level, ts, message) VALUES (?, ?, ?, ?)",
                (task_id, level, time.time(), message),
            )

    async def tail_logs(self, task_id: str, n: int = 50) -> list[LogEntry]:
        async with self._conn.execute(
            "SELECT level, ts, message FROM logs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (task_id, n),
        ) as cur:
            rows = await cur.fetchall()
        return [LogEntry(level=r["level"], ts=r["ts"], message=r["message"]) for r in reversed(rows)]

    # ---------- per-chat session ----------

    async def set_chat_repo(self, chat_id: int, repo_path: str | None) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO sessions (chat_id, repo_path, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     repo_path = excluded.repo_path,
                     updated_at = excluded.updated_at""",
                (chat_id, repo_path, time.time()),
            )

    async def set_chat_last_task(self, chat_id: int, task_id: str) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO sessions (chat_id, last_task_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     last_task_id = excluded.last_task_id,
                     updated_at = excluded.updated_at""",
                (chat_id, task_id, time.time()),
            )

    async def set_chat_model(
        self,
        chat_id: int,
        worker: str,  # "planner" | "implementer" | "reviewer" | "chat"
        model: str | None,
    ) -> None:
        if worker not in ("planner", "implementer", "reviewer", "chat"):
            raise ValueError(f"unknown worker: {worker}")
        col = f"{worker}_model"
        async with self._tx() as c:
            await c.execute(
                f"""INSERT INTO sessions (chat_id, {col}, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                      {col} = excluded.{col},
                      updated_at = excluded.updated_at""",
                (chat_id, model, time.time()),
            )

    async def set_chat_effort(
        self,
        chat_id: int,
        worker: str,  # "planner" | "reviewer" | "triage" | "implementer"
        effort: str | None,  # "low"|"medium"|"high"|"xhigh"|"max" or None to clear
    ) -> None:
        if worker not in ("planner", "reviewer", "triage", "implementer"):
            raise ValueError(f"effort not applicable to worker: {worker}")
        col = f"{worker}_effort"
        async with self._tx() as c:
            await c.execute(
                f"""INSERT INTO sessions (chat_id, {col}, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                      {col} = excluded.{col},
                      updated_at = excluded.updated_at""",
                (chat_id, effort, time.time()),
            )

    async def set_chat_temperature(
        self,
        chat_id: int,
        worker: str,  # "planner" | "implementer" | "reviewer" | "chat"
        temperature: float | None,
    ) -> None:
        if worker not in ("planner", "implementer", "reviewer", "chat"):
            raise ValueError(f"temperature not applicable to worker: {worker}")
        col = f"{worker}_temperature"
        async with self._tx() as c:
            await c.execute(
                f"""INSERT INTO sessions (chat_id, {col}, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                      {col} = excluded.{col},
                      updated_at = excluded.updated_at""",
                (chat_id, temperature, time.time()),
            )

    # ---------- chat history (conversational memory) ----------

    async def append_chat_message(
        self,
        chat_id: int,
        role: str,
        text: str,
        *,
        max_keep: int | None = None,
        file_path: str | None = None,
        file_content: str | None = None,
        file_classification: dict | None = None,
    ) -> int:
        """Append one chat entry. By default keeps everything (`max_keep=None`)
        — only `/forget` removes entries. Pass `max_keep=N` to enable a
        rolling window (used by tests). File-attachments can be inlined via
        `file_path` (where the file lives), `file_content` (text up to ~30KB),
        and `file_classification` (dict from `_classify_uploaded_json`)."""
        if role not in ("user", "bot"):
            raise ValueError(f"unknown role: {role}")
        text = (text or "").strip()
        if not text and not file_content:
            return 0
        # Cap inline content at 30KB so a single bad upload can't bloat the DB.
        if file_content is not None and len(file_content) > 30_000:
            file_content = file_content[:30_000]
        cls_json = json.dumps(file_classification) if file_classification else None
        async with self._tx() as c:
            cur = await c.execute(
                """INSERT INTO chat_messages
                     (chat_id, role, text, ts, file_path, file_content,
                      file_classification, summarized)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    chat_id, role, text[:4000], time.time(),
                    file_path, file_content, cls_json,
                ),
            )
            new_id = cur.lastrowid or 0
            if max_keep is not None:
                await c.execute(
                    """DELETE FROM chat_messages
                       WHERE chat_id = ? AND id NOT IN (
                         SELECT id FROM chat_messages WHERE chat_id = ?
                         ORDER BY id DESC LIMIT ?
                       )""",
                    (chat_id, chat_id, max_keep),
                )
        return new_id

    async def recent_chat_messages(
        self, chat_id: int, limit: int = 12
    ) -> list[dict[str, Any]]:
        async with self._conn.execute(
            """SELECT id, role, text, ts, file_path, file_content,
                      file_classification
               FROM chat_messages WHERE chat_id = ?
               ORDER BY id DESC LIMIT ?""",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in reversed(rows):
            cls = None
            raw = r["file_classification"]
            if raw:
                try:
                    cls = json.loads(raw)
                except Exception:
                    cls = None
            out.append({
                "id": r["id"],
                "role": r["role"],
                "text": r["text"],
                "ts": r["ts"],
                "file_path": r["file_path"],
                "file_content": r["file_content"],
                "file_classification": cls,
            })
        return out

    async def search_chat_messages(
        self, chat_id: int, query: str, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Full-text search over chat_messages (text + file_content) using
        FTS5 if available, falling back to LIKE. Returns the same shape as
        `recent_chat_messages`, ranked by relevance (FTS) or recency (LIKE)."""
        q = (query or "").strip()
        if not q:
            return []
        # Try FTS5 first.
        try:
            async with self._conn.execute(
                """SELECT m.id, m.role, m.text, m.ts, m.file_path,
                          m.file_content, m.file_classification
                   FROM chat_messages_fts f
                   JOIN chat_messages m ON m.id = f.rowid
                   WHERE m.chat_id = ?
                     AND chat_messages_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (chat_id, q, limit),
            ) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            # FTS5 not built or syntax error in user query — fall back.
            like = f"%{q}%"
            async with self._conn.execute(
                """SELECT id, role, text, ts, file_path, file_content,
                          file_classification
                   FROM chat_messages
                   WHERE chat_id = ?
                     AND (text LIKE ? OR COALESCE(file_content,'') LIKE ?)
                   ORDER BY id DESC LIMIT ?""",
                (chat_id, like, like, limit),
            ) as cur:
                rows = await cur.fetchall()
        out = []
        for r in rows:
            cls = None
            raw = r["file_classification"]
            if raw:
                try:
                    cls = json.loads(raw)
                except Exception:
                    cls = None
            out.append({
                "id": r["id"],
                "role": r["role"],
                "text": r["text"],
                "ts": r["ts"],
                "file_path": r["file_path"],
                "file_content": r["file_content"],
                "file_classification": cls,
            })
        return out

    # ---------- chat summaries (warm tier) ----------

    async def add_chat_summary(
        self,
        chat_id: int,
        *,
        period_from: float,
        period_to: float,
        summary: str,
        msg_count: int,
    ) -> int:
        async with self._tx() as c:
            cur = await c.execute(
                """INSERT INTO chat_summaries
                     (chat_id, period_from, period_to, summary, msg_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chat_id, period_from, period_to, summary, msg_count, time.time()),
            )
            return cur.lastrowid or 0

    async def recent_chat_summaries(
        self, chat_id: int, *, limit: int = 5,
    ) -> list[dict[str, Any]]:
        async with self._conn.execute(
            """SELECT period_from, period_to, summary, msg_count, created_at
               FROM chat_summaries WHERE chat_id = ?
               ORDER BY period_to DESC LIMIT ?""",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def mark_messages_summarized(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        async with self._tx() as c:
            await c.execute(
                f"UPDATE chat_messages SET summarized=1 WHERE id IN ({placeholders})",
                ids,
            )

    # ---------- pending attachments (24h sliding) ----------

    async def add_pending_attachment(
        self,
        chat_id: int,
        *,
        file_name: str,
        file_path: str,
        classification: dict | None = None,
    ) -> int:
        cls_json = json.dumps(classification) if classification else None
        async with self._tx() as c:
            cur = await c.execute(
                """INSERT INTO pending_attachments
                     (chat_id, file_name, file_path, classification, received_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, file_name, file_path, cls_json, time.time()),
            )
            return cur.lastrowid or 0

    async def list_pending_attachments(
        self, chat_id: int, *, hours: int = 24, only_unhandled: bool = False,
    ) -> list[dict[str, Any]]:
        cutoff = time.time() - hours * 3600
        q = (
            """SELECT id, file_name, file_path, classification, received_at,
                      handled, handled_by_task_id
               FROM pending_attachments
               WHERE chat_id = ? AND received_at >= ?"""
        )
        if only_unhandled:
            q += " AND handled = 0"
        q += " ORDER BY received_at DESC"
        async with self._conn.execute(q, (chat_id, cutoff)) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            cls = None
            raw = r["classification"]
            if raw:
                try:
                    cls = json.loads(raw)
                except Exception:
                    cls = None
            out.append({
                "id": r["id"],
                "file_name": r["file_name"],
                "file_path": r["file_path"],
                "classification": cls,
                "received_at": r["received_at"],
                "handled": bool(r["handled"]),
                "handled_by_task_id": r["handled_by_task_id"],
            })
        return out

    async def mark_attachment_handled(
        self, attachment_id: int, *, task_id: str | None = None,
    ) -> None:
        async with self._tx() as c:
            await c.execute(
                """UPDATE pending_attachments
                   SET handled = 1, handled_by_task_id = ?
                   WHERE id = ?""",
                (task_id, attachment_id),
            )

    async def cleanup_pending_attachments(self, hours: int = 24) -> int:
        cutoff = time.time() - hours * 3600
        async with self._tx() as c:
            cur = await c.execute(
                "DELETE FROM pending_attachments WHERE received_at < ?", (cutoff,),
            )
            return cur.rowcount or 0

    # ---------- chat questions (human-in-the-loop) ----------

    async def create_chat_question(
        self, chat_id: int, question: str, *, task_id: str | None = None
    ) -> int:
        async with self._tx() as c:
            cur = await c.execute(
                "INSERT INTO chat_questions (chat_id, task_id, question, asked_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, task_id, question, time.time()),
            )
            return cur.lastrowid or 0

    async def get_pending_question(self, chat_id: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM chat_questions "
            "WHERE chat_id = ? AND answered_at IS NULL AND expired_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_question(self, qid: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM chat_questions WHERE id = ?", (qid,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def answer_chat_question(self, qid: int, answer: str) -> None:
        async with self._tx() as c:
            await c.execute(
                "UPDATE chat_questions SET answer = ?, answered_at = ? WHERE id = ?",
                (answer, time.time(), qid),
            )

    async def set_user_fact(self, chat_id: int, key: str, value: str) -> None:
        """Persist a per-chat ground-truth fact (e.g. project path).
        Used by the smart-document handler when staging files etc."""
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO user_facts (chat_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id, key) DO UPDATE SET
                     value = excluded.value,
                     updated_at = excluded.updated_at""",
                (chat_id, key, value, time.time()),
            )

    async def get_user_facts(self, chat_id: int) -> dict[str, str]:
        async with self._conn.execute(
            "SELECT key, value FROM user_facts WHERE chat_id = ? "
            "ORDER BY updated_at DESC LIMIT 50",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def delete_user_fact(self, chat_id: int, key: str) -> bool:
        async with self._tx() as c:
            cur = await c.execute(
                "DELETE FROM user_facts WHERE chat_id = ? AND key = ?",
                (chat_id, key),
            )
            return (cur.rowcount or 0) > 0

    async def expire_chat_question(self, qid: int) -> None:
        async with self._tx() as c:
            await c.execute(
                "UPDATE chat_questions SET expired_at = ? WHERE id = ?",
                (time.time(), qid),
            )

    async def clear_chat_messages(self, chat_id: int) -> int:
        """Wipes ALL conversational memory for one chat: messages, summaries,
        pending attachments. Used by `/forget`."""
        async with self._tx() as c:
            cur = await c.execute(
                "DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,)
            )
            n = cur.rowcount or 0
            await c.execute(
                "DELETE FROM chat_summaries WHERE chat_id = ?", (chat_id,)
            )
            await c.execute(
                "DELETE FROM pending_attachments WHERE chat_id = ?", (chat_id,)
            )
            return n

    # ---------- skills ----------

    async def create_skill(
        self,
        *,
        name: str,
        description: str | None,
        task_template: str,
        rationale: str | None = None,
        source_task_ids: list[str] | None = None,
    ) -> int:
        async with self._tx() as c:
            cur = await c.execute(
                """INSERT INTO skills (name, description, task_template, rationale,
                                       source_task_ids, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    description,
                    task_template,
                    rationale,
                    json.dumps(source_task_ids or []),
                    time.time(),
                ),
            )
            return cur.lastrowid or 0

    async def list_skills(self) -> list[dict]:
        async with self._conn.execute(
            "SELECT * FROM skills ORDER BY usage_count DESC, created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_skill_by_name(self, name: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM skills WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_skill(self, name: str) -> bool:
        async with self._tx() as c:
            cur = await c.execute("DELETE FROM skills WHERE name = ?", (name,))
            return (cur.rowcount or 0) > 0

    async def update_skill(
        self,
        name: str,
        *,
        description: str | None = None,
        task_template: str | None = None,
        rationale: str | None = None,
    ) -> bool:
        """Patch description / task_template / rationale of an existing skill.
        Returns True if a row was updated. Used by /skillupgrade so Opus can
        improve a skill's wording without losing its name + usage_count."""
        sets, vals = [], []
        if description is not None:
            sets.append("description = ?")
            vals.append(description)
        if task_template is not None:
            sets.append("task_template = ?")
            vals.append(task_template)
        if rationale is not None:
            sets.append("rationale = ?")
            vals.append(rationale)
        if not sets:
            return False
        vals.append(name)
        async with self._tx() as c:
            cur = await c.execute(
                f"UPDATE skills SET {', '.join(sets)} WHERE name = ?",
                vals,
            )
            return (cur.rowcount or 0) > 0

    async def increment_skill_usage(self, name: str) -> None:
        async with self._tx() as c:
            await c.execute(
                "UPDATE skills SET usage_count = usage_count + 1, last_used_at = ? WHERE name = ?",
                (time.time(), name),
            )

    async def record_skill_suggestion(
        self,
        task_id: str,
        suggestion: dict,
        chat_id: int | None,
    ) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT OR REPLACE INTO skill_suggestions
                   (task_id, suggestion_json, chat_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (task_id, json.dumps(suggestion), chat_id, time.time()),
            )

    async def get_skill_suggestion(self, task_id: str) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM skill_suggestions WHERE task_id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        out = dict(row)
        out["suggestion"] = json.loads(out.pop("suggestion_json"))
        return out

    async def mark_skill_suggestion_decided(self, task_id: str, decision: str) -> None:
        async with self._tx() as c:
            await c.execute(
                "UPDATE skill_suggestions SET decision = ?, decided_at = ? WHERE task_id = ?",
                (decision, time.time(), task_id),
            )

    async def set_chat_replan_max(self, chat_id: int, replan_max: int | None) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO sessions (chat_id, replan_max, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     replan_max = excluded.replan_max,
                     updated_at = excluded.updated_at""",
                (chat_id, replan_max, time.time()),
            )

    async def set_chat_lang(self, chat_id: int, lang: str | None) -> None:
        if lang is not None and lang not in ("de", "en"):
            raise ValueError(f"unknown lang: {lang}")
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO sessions (chat_id, lang, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     lang = excluded.lang,
                     updated_at = excluded.updated_at""",
                (chat_id, lang, time.time()),
            )

    async def set_chat_max_iterations(
        self, chat_id: int, max_iterations: int | None
    ) -> None:
        async with self._tx() as c:
            await c.execute(
                """INSERT INTO sessions (chat_id, max_iterations, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     max_iterations = excluded.max_iterations,
                     updated_at = excluded.updated_at""",
                (chat_id, max_iterations, time.time()),
            )

    async def set_chat_int_setting(
        self, chat_id: int, column: str, value: int | None,
    ) -> None:
        """Generic setter for any whitelisted INTEGER per-chat setting.
        Used by the /toggle commands and /failsbeforereplan."""
        allowed = {
            "replan_after_failures",
            "triage_enabled",
            "auto_skill_suggest",
            "context7_enabled",
            "websearch_enabled",
            "auto_decompose",
            "max_subtasks",
            "multiplan_enabled",
        }
        if column not in allowed:
            raise ValueError(f"unknown int setting: {column}")
        async with self._tx() as c:
            await c.execute(
                f"""INSERT INTO sessions (chat_id, {column}, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                      {column} = excluded.{column},
                      updated_at = excluded.updated_at""",
                (chat_id, value, time.time()),
            )

    async def get_chat_session(self, chat_id: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "chat_id": row["chat_id"],
            "repo_path": row["repo_path"],
            "last_task_id": row["last_task_id"],
            "planner_model": row["planner_model"],
            "implementer_model": row["implementer_model"],
            "reviewer_model": row["reviewer_model"],
            "chat_model": row["chat_model"],
            "planner_effort": row["planner_effort"],
            "reviewer_effort": row["reviewer_effort"],
            "triage_effort": row["triage_effort"],
            "implementer_effort": row["implementer_effort"],
            "planner_temperature": row["planner_temperature"],
            "implementer_temperature": row["implementer_temperature"],
            "reviewer_temperature": row["reviewer_temperature"],
            "chat_temperature": row["chat_temperature"],
            "replan_max": row["replan_max"],
            "max_iterations": row["max_iterations"],
            "replan_after_failures": row["replan_after_failures"],
            "triage_enabled": row["triage_enabled"],
            "auto_skill_suggest": row["auto_skill_suggest"],
            "context7_enabled": row["context7_enabled"],
            "websearch_enabled": row["websearch_enabled"],
            "lang": row["lang"],
            "auto_decompose": row["auto_decompose"],
            "max_subtasks": row["max_subtasks"],
            "updated_at": row["updated_at"],
        }
