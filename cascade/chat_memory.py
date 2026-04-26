"""ChatMemory — persistent conversational context per chat.

Three tiers feed the triage layer's system prompt:

1. **Hot** — last `limit_hot` messages (default 30) verbatim, including
   inlined file_content (up to 30KB per file). This is the bot's working
   memory: what was just said + what files were just shared.
2. **Warm** — older messages get compressed into `chat_summaries` rows by
   a background task once they age past ~7 days. Summaries are kept
   forever; the original messages stay in `chat_messages` (retention is
   unlimited per user preference) and can be searched via FTS5.
3. **Long** — RLM (`memory.recall_context`) provides cross-chat / cross-
   session recall. Already integrated; we layer it on top.

Plus: `pending_attachments` (24h sliding window) is included separately
so the bot can answer "did you receive a JSON earlier?" even when the
file message has scrolled out of the Hot tier.

The point of this module is `build_context(chat_id, query=...)` — it
returns a ready-to-paste markdown block that gets concatenated into
the triage / chat-worker system prompt. All formatting decisions
live here so callers stay simple.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .store import Store


log = logging.getLogger("cascade.chat_memory")


@dataclass
class ChatMemoryConfig:
    limit_hot: int = 30
    limit_warm: int = 5
    limit_fts: int = 8
    file_content_chars: int = 1500     # how much of each inlined file to show
    pending_attachments_hours: int = 24
    summarize_after_days: float = 7.0
    summarize_batch_size: int = 50


_DEFAULT = ChatMemoryConfig()


class ChatMemory:
    """Thin orchestration layer over Store. Holds no state itself."""

    def __init__(self, store: Store, *, config: ChatMemoryConfig | None = None) -> None:
        self.store = store
        self.config = config or _DEFAULT

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    async def append(
        self,
        chat_id: int,
        role: str,
        text: str,
        *,
        file_path: str | None = None,
        file_content: str | None = None,
        file_classification: dict | None = None,
        register_attachment: bool = True,
    ) -> int:
        """Persist one chat turn. If a file is attached, also registers it
        in `pending_attachments` (unless `register_attachment=False`).
        Returns the new chat_messages.id."""
        msg_id = await self.store.append_chat_message(
            chat_id, role, text,
            file_path=file_path,
            file_content=file_content,
            file_classification=file_classification,
        )
        if file_path and register_attachment:
            try:
                file_name = file_path.rsplit("/", 1)[-1]
                await self.store.add_pending_attachment(
                    chat_id,
                    file_name=file_name,
                    file_path=file_path,
                    classification=file_classification,
                )
            except Exception as e:  # never let memory failures break the bot
                log.warning("add_pending_attachment failed: %s", e)
        return msg_id

    # ------------------------------------------------------------------
    # Build context for triage / chat-worker
    # ------------------------------------------------------------------

    async def build_context(
        self,
        chat_id: int,
        *,
        query: str | None = None,
        lang: str = "de",
    ) -> str:
        """Assemble a structured context block for the chat / triage prompt.

        Sections (only emitted if non-empty):
          - USER FACTS (persistent ground-truth)
          - RECENT UPLOADS (last 24h, including handled state)
          - CONVERSATION (Hot tier, with inlined file content)
          - EARLIER (Warm tier — chat_summaries)
          - SEARCH HITS (FTS5 matches for `query` outside the Hot tier)
        """
        cfg = self.config
        sections: list[str] = []

        # USER FACTS
        try:
            facts = await self.store.get_user_facts(chat_id)
        except Exception:
            facts = {}
        if facts:
            head = "USER FACTS (persistent)" if lang != "de" else "NUTZER-FAKTEN (persistent)"
            lines = [f"=== {head} ==="]
            for k, v in facts.items():
                lines.append(f"- {k}: {v}")
            sections.append("\n".join(lines))

        # RECENT UPLOADS
        try:
            atts = await self.store.list_pending_attachments(
                chat_id, hours=cfg.pending_attachments_hours,
            )
        except Exception:
            atts = []
        if atts:
            head = (
                f"RECENT UPLOADS (last {cfg.pending_attachments_hours}h)"
                if lang != "de"
                else f"KüRZLICH HOCHGELADENE DATEIEN (letzte {cfg.pending_attachments_hours}h)"
            )
            lines = [f"=== {head} ==="]
            for a in atts:
                cls = a.get("classification") or {}
                kind = cls.get("kind") or "?"
                summary = cls.get("summary") or ""
                handled = "[handled]" if a.get("handled") else "[pending]"
                ts_h = _hhmm(a["received_at"])
                lines.append(
                    f"- {a['file_name']} ({kind}) — {a['file_path']} "
                    f"{handled} {ts_h}{(' — ' + summary) if summary else ''}"
                )
            sections.append("\n".join(lines))

        # CONVERSATION (Hot tier)
        try:
            hot = await self.store.recent_chat_messages(chat_id, limit=cfg.limit_hot)
        except Exception:
            hot = []
        hot_ids = {m["id"] for m in hot}
        if hot:
            head = (
                f"CONVERSATION (last {len(hot)})"
                if lang != "de"
                else f"CHAT-VERLAUF (letzte {len(hot)})"
            )
            lines = [f"=== {head} ==="]
            for m in hot:
                tag = "User" if m["role"] == "user" else "Bot"
                txt = (m["text"] or "").replace("\n", " ").strip()
                if len(txt) > 600:
                    txt = txt[:600] + "…"
                ts_h = _hhmm(m["ts"])
                lines.append(f"[{ts_h}] {tag}: {txt}")
                fc = m.get("file_content")
                fp = m.get("file_path")
                fcls = m.get("file_classification") or {}
                if fp or fc:
                    kind = fcls.get("kind") or "file"
                    meta_bits = [kind]
                    if fp:
                        meta_bits.append(fp)
                    lines.append(f"  [FILE: {' — '.join(meta_bits)}]")
                if fc:
                    snippet = fc.strip()
                    if len(snippet) > cfg.file_content_chars:
                        snippet = snippet[:cfg.file_content_chars] + "\n…[truncated]"
                    lines.append("  ```")
                    for sl in snippet.splitlines():
                        lines.append(f"  {sl}")
                    lines.append("  ```")
            sections.append("\n".join(lines))

        # EARLIER (Warm tier)
        try:
            warm = await self.store.recent_chat_summaries(chat_id, limit=cfg.limit_warm)
        except Exception:
            warm = []
        if warm:
            head = (
                "EARLIER CONVERSATIONS (summaries)"
                if lang != "de"
                else "FRÜHERER CHAT-VERLAUF (Zusammenfassungen)"
            )
            lines = [f"=== {head} ==="]
            for w in warm:
                pf = _ymd(w["period_from"])
                pt = _ymd(w["period_to"])
                lines.append(f"- {pf} — {pt} ({w['msg_count']} msgs): {w['summary'][:500]}")
            sections.append("\n".join(lines))

        # SEARCH HITS (FTS over messages NOT in Hot)
        if query and query.strip():
            try:
                hits = await self.store.search_chat_messages(
                    chat_id, query, limit=cfg.limit_fts,
                )
            except Exception:
                hits = []
            extra = [h for h in hits if h["id"] not in hot_ids]
            if extra:
                head = (
                    f"SEARCH HITS (query={query!r})"
                    if lang != "de"
                    else f"SUCH-TREFFER (Suche={query!r})"
                )
                lines = [f"=== {head} ==="]
                for h in extra[: cfg.limit_fts]:
                    tag = "User" if h["role"] == "user" else "Bot"
                    ts_h = _ymd(h["ts"])
                    txt = (h["text"] or "").replace("\n", " ").strip()
                    if len(txt) > 300:
                        txt = txt[:300] + "…"
                    fp = h.get("file_path")
                    file_marker = f" [FILE: {fp}]" if fp else ""
                    lines.append(f"- {ts_h} {tag}: {txt}{file_marker}")
                sections.append("\n".join(lines))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Background: summarize old messages (called from a periodic task)
    # ------------------------------------------------------------------

    async def candidates_for_summary(
        self, chat_id: int,
    ) -> list[dict]:
        """Return un-summarized messages older than `summarize_after_days`,
        oldest first, capped at `summarize_batch_size`. The actual summary
        generation is done outside this module (needs an LLM)."""
        cutoff = time.time() - self.config.summarize_after_days * 86400
        # We keep this query inline — small enough not to warrant a method
        # on Store.
        async with self.store._conn.execute(
            """SELECT id, role, text, ts, file_path, file_content
               FROM chat_messages
               WHERE chat_id = ? AND summarized = 0 AND ts < ?
               ORDER BY id ASC LIMIT ?""",
            (chat_id, cutoff, self.config.summarize_batch_size),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _ymd(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
