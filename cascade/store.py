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

CREATE TABLE IF NOT EXISTS sessions (
    chat_id     INTEGER PRIMARY KEY,
    repo_path   TEXT,
    last_task_id TEXT,
    planner_model TEXT,
    implementer_model TEXT,
    reviewer_model TEXT,
    planner_effort TEXT,
    reviewer_effort TEXT,
    triage_effort TEXT,
    replan_max INTEGER,
    updated_at  REAL NOT NULL
);
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
        worker: str,  # "planner" | "implementer" | "reviewer"
        model: str | None,
    ) -> None:
        if worker not in ("planner", "implementer", "reviewer"):
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
        worker: str,  # "planner" | "reviewer" | "triage"
        effort: str | None,  # "low"|"medium"|"high"|"xhigh"|"max" or None to clear
    ) -> None:
        if worker not in ("planner", "reviewer", "triage"):
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
            "planner_effort": row["planner_effort"],
            "reviewer_effort": row["reviewer_effort"],
            "triage_effort": row["triage_effort"],
            "replan_max": row["replan_max"],
            "updated_at": row["updated_at"],
        }
