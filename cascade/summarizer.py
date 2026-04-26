"""Background chat-summarisation worker.

ChatMemory keeps a Hot tier (last 30 messages, verbatim) and a Warm tier
(`chat_summaries` rows). Older messages remain in `chat_messages` for
full-text search but the Triage-Layer's `build_context()` only injects
summaries for them — much cheaper than dumping thousands of raw lines
into every prompt.

This module owns the *creation* of those summaries. It runs as an
asyncio.Task spawned by `bot/lifecycle.post_init` and ticks every
`tick_interval_s` (default 6h):

    1. For every chat_id that has ANY un-summarised message older than
       `summarize_after_days` (default 7), fetch up to `batch_size` of
       them oldest-first.
    2. Ask Sonnet to compress them into ~5 sentences keeping concrete
       anchors (file names, decisions, task IDs).
    3. Persist as a `chat_summaries` row spanning the messages' time
       window, then mark each input row as `summarized=1`.

Best-effort: any LLM error → log + skip that batch. Never crashes the
bot. Disabled via `cascade_summarize_enabled=false`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .chat_memory import ChatMemory, ChatMemoryConfig
from .config import Settings, settings
from .llm_client import LLMClientError, agent_chat
from .store import Store

log = logging.getLogger("cascade.summarizer")


_SYSTEM_DE = """Du fasst einen Telegram-Chat-Verlauf zwischen einem User und
einem Coding-Bot zusammen. Behalte konkrete Anker: Dateinamen, Pfade,
Task-IDs, Projektnamen, getroffene Entscheidungen. Stil knapp, neutral,
~5 Sätze. Antworte ausschließlich mit dem Text — keine JSON-Hülle,
kein Markdown."""


_SYSTEM_EN = """Compress a Telegram chat between a user and a coding bot
into ~5 sentences. Keep concrete anchors: file names, paths, task IDs,
project names, decisions made. Plain text only — no JSON envelope,
no markdown."""


async def summarize_batch(
    store: Store,
    chat_id: int,
    batch: list[dict],
    *,
    s: Settings | None = None,
    lang: str = "de",
) -> str | None:
    """Ask Sonnet to compress `batch` into a short summary string. Returns
    None on LLM error. Caller persists the summary."""
    if not batch:
        return None
    s = s or settings()
    lines = []
    for m in batch:
        ts_h = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        tag = "User" if m["role"] == "user" else "Bot"
        text = (m["text"] or "").replace("\n", " ").strip()
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"[{ts_h}] {tag}: {text}")
    prompt = "\n".join(lines)
    try:
        raw = await agent_chat(
            prompt=prompt,
            model=s.cascade_reviewer_model,
            system_prompt=_SYSTEM_DE if lang == "de" else _SYSTEM_EN,
            output_json=False,
            timeout_s=120,
            # Tight retry budget — summaries are best-effort housekeeping.
            retry_max_total_wait_s=300.0,
            retry_min_backoff_s=15.0,
            retry_max_backoff_s=60.0,
            s=s,
        )
    except LLMClientError as e:
        log.warning("summarize_batch llm error chat=%s: %s", chat_id, e)
        return None
    return (raw or "").strip()


async def run_one_pass(
    store: Store,
    *,
    s: Settings | None = None,
    config: ChatMemoryConfig | None = None,
) -> int:
    """Walk every chat_id with un-summarised old messages and emit one
    summary row per batch. Returns the number of rows added."""
    s = s or settings()
    cm = ChatMemory(store, config=config)
    # Find every chat_id that has any un-summarised row older than the
    # cutoff. Inline SQL — small enough not to warrant a Store method.
    cutoff = time.time() - cm.config.summarize_after_days * 86400
    async with store._conn.execute(
        """SELECT DISTINCT chat_id FROM chat_messages
           WHERE summarized = 0 AND ts < ?""",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    chats = [int(r["chat_id"]) for r in rows]
    if not chats:
        return 0
    added = 0
    for chat_id in chats:
        # Optional per-chat lang
        try:
            sess = await store.get_chat_session(chat_id) or {}
            lang = sess.get("lang") or s.cascade_bot_lang or "de"
        except Exception:
            lang = "de"
        cands = await cm.candidates_for_summary(chat_id)
        if not cands:
            continue
        summary = await summarize_batch(store, chat_id, cands, s=s, lang=lang)
        if not summary:
            continue
        try:
            await store.add_chat_summary(
                chat_id,
                period_from=cands[0]["ts"],
                period_to=cands[-1]["ts"],
                summary=summary,
                msg_count=len(cands),
            )
            await store.mark_messages_summarized([c["id"] for c in cands])
            added += 1
            log.info(
                "summarized chat=%s n=%d window=%s..%s",
                chat_id, len(cands),
                time.strftime("%Y-%m-%d", time.localtime(cands[0]["ts"])),
                time.strftime("%Y-%m-%d", time.localtime(cands[-1]["ts"])),
            )
        except Exception as e:
            log.warning("summarize_persist failed chat=%s: %s", chat_id, e)
    return added


async def background_loop(
    store: Store,
    *,
    tick_interval_s: float = 6 * 3600,
    s: Settings | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Long-running coroutine — spawn with asyncio.create_task in
    `lifecycle.post_init`."""
    s = s or settings()
    if not getattr(s, "cascade_summarize_enabled", True):
        log.info("summarizer disabled via cascade_summarize_enabled=false")
        return
    log.info("summarizer started — tick every %.0fs", tick_interval_s)
    # Slight initial delay so we don't compete with the bot's own startup work.
    try:
        await asyncio.wait_for(
            stop_event.wait() if stop_event else asyncio.sleep(60),
            timeout=60.0,
        )
        if stop_event and stop_event.is_set():
            return
    except asyncio.TimeoutError:
        pass
    while True:
        try:
            n = await run_one_pass(store, s=s)
            if n:
                log.info("summarizer pass: %d new summary rows", n)
        except Exception as e:
            log.warning("summarizer pass crashed: %s", e)
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_interval_s)
                if stop_event.is_set():
                    return
            else:
                await asyncio.sleep(tick_interval_s)
        except asyncio.TimeoutError:
            continue
