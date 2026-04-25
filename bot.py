"""Telegram bot interface for Claude-Cascade.

Usage:
  python bot.py

Required env (.env):
  TELEGRAM_BOT_TOKEN   — from @BotFather
  TELEGRAM_OWNER_ID    — numeric Telegram user id of the sole authorized user
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from cascade.config import settings
from cascade.core import maintenance, run_cascade
from cascade.i18n import t
from cascade.store import Store

log = logging.getLogger("cascade.bot")

# Per-chat in-flight task registry: chat_id → (task_id, asyncio.Task, cancel_event)
_INFLIGHT: dict[int, tuple[str, asyncio.Task, asyncio.Event]] = {}

# Per-chat language override (chat_id → "de"|"en"); falls back to settings.cascade_bot_lang.
_LANG_OVERRIDE: dict[int, str] = {}

GIT_WHITELIST = {"status", "log", "diff", "branch", "checkout", "pull", "push", "commit", "show"}


def _lang(update: Update) -> str:
    if update.effective_chat and update.effective_chat.id in _LANG_OVERRIDE:
        return _LANG_OVERRIDE[update.effective_chat.id]
    return settings().cascade_bot_lang


# ------------------------------------------------------------------ helpers


def _is_owner(update: Update) -> bool:
    s = settings()
    user = update.effective_user
    return bool(user and s.telegram_owner_id and user.id == s.telegram_owner_id)


async def _owner_only(update: Update, _ctx) -> bool:
    if not _is_owner(update):
        log.warning("ignored unauthorized user id=%s", getattr(update.effective_user, "id", "?"))
        return False
    return True


async def _send(message: Message, text: str, *, code: bool = False) -> Message:
    if code:
        text = f"```\n{text[:3900]}\n```"
        return await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return await message.reply_text(text[:4000])


def _fmt_status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "running": "🔁",
        "interrupted": "⏸",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }.get(status, "•")


# ------------------------------------------------------------------ task runner


async def _run_task_for_chat(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    task_text: str,
    *,
    attachments: list[Path] | None = None,
    resume_task_id: str | None = None,
) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    s = settings()

    store: Store = ctx.application.bot_data["store"]
    sess = await store.get_chat_session(chat.id) if chat else None
    repo = Path(sess["repo_path"]) if sess and sess.get("repo_path") else None

    lang = _lang(update)
    initial = t("progress.planning_initial", lang=lang)
    status_msg = await msg.reply_text(initial)
    cancel = asyncio.Event()

    state = {"last_text": initial}

    async def progress(task_id: str, event: str, payload: dict) -> None:
        line = _format_progress_line(event, payload, lang)
        if not line:
            return
        new_text = state["last_text"] + "\n" + line
        if len(new_text) > 3500:
            new_text = "…\n" + new_text[-3400:]
        state["last_text"] = new_text
        try:
            await status_msg.edit_text(new_text)
        except Exception:
            pass

    coro = run_cascade(
        task=task_text,
        source="telegram",
        repo=repo,
        attachments=attachments,
        progress=progress,
        s=s,
        store=store,
        cancel_event=cancel,
        resume_task_id=resume_task_id,
    )
    task_obj = asyncio.create_task(coro)

    # Stash cancel handle once we know the task_id (after first store write). We poll briefly.
    async def register_when_known() -> None:
        for _ in range(30):  # up to ~3s
            await asyncio.sleep(0.1)
            latest = await store.latest_task()
            if latest and latest.task_text == task_text:
                _INFLIGHT[chat.id] = (latest.id, task_obj, cancel)
                if chat:
                    await store.set_chat_last_task(chat.id, latest.id)
                return

    asyncio.create_task(register_when_known())

    try:
        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
        result = await task_obj
    except asyncio.CancelledError:
        await msg.reply_text(t("result.cancelled", lang=lang))
        return
    except Exception as e:
        await msg.reply_text(t("result.crashed", lang=lang, error=str(e)))
        return
    finally:
        _INFLIGHT.pop(chat.id, None)

    await msg.reply_text(
        t(
            "result.summary",
            lang=lang,
            emoji=_fmt_status_emoji(result.status),
            status=result.status,
            task_id=result.task_id,
            iterations=result.iterations,
            workspace=result.workspace_path,
            summary=result.summary,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


def _format_progress_line(event: str, payload: dict, lang: str = "de") -> str | None:
    if event == "started":
        return ""
    if event == "planning":
        return t("progress.planning", lang=lang)
    if event == "planned":
        return t("progress.planned", lang=lang, summary=(payload.get("summary") or "")[:120])
    if event == "implementing":
        return t("progress.implementing", lang=lang, n=payload.get("iteration"))
    if event == "implemented":
        return t(
            "progress.implemented",
            lang=lang,
            n=payload.get("iteration"),
            ops=payload.get("ops", 0),
            failed=payload.get("failed", 0),
        )
    if event == "reviewing":
        return t("progress.reviewing", lang=lang, n=payload.get("iteration"))
    if event == "reviewed":
        fb = (payload.get("feedback") or "").strip().splitlines()[0:1]
        suffix = f": {fb[0][:120]}" if fb else ""
        key = "progress.reviewed_pass" if payload.get("pass") else "progress.reviewed_fail"
        return t(key, lang=lang, n=payload.get("iteration"), suffix=suffix)
    if event in ("iteration_failed", "done"):
        return ""
    if event == "failed":
        return t("progress.failed", lang=lang, summary=payload.get("summary", ""))
    return None


# ------------------------------------------------------------------ handlers


async def cmd_help(update: Update, _ctx) -> None:
    if not await _owner_only(update, _ctx):
        return
    await update.effective_message.reply_text(t("help", lang=_lang(update)), parse_mode=ParseMode.MARKDOWN)


async def cmd_lang(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    args = ctx.args or []
    chat_id = update.effective_chat.id
    current = _LANG_OVERRIDE.get(chat_id, settings().cascade_bot_lang)
    if not args:
        await update.effective_message.reply_text(
            t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN
        )
        return
    new = args[0].lower()
    if new not in ("de", "en"):
        await update.effective_message.reply_text(t("lang.usage", lang=current, current=current), parse_mode=ParseMode.MARKDOWN)
        return
    _LANG_OVERRIDE[chat_id] = new
    # template uses {lang} which collides with our `lang` kwarg — pre-format manually.
    msg_template = "Sprache auf `{}` umgestellt." if new == "de" else "Language switched to `{}`."
    await update.effective_message.reply_text(msg_template.format(new), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args:
        task = await store.get_task(args[0])
    else:
        task = await store.latest_task()
    if not task:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    await update.effective_message.reply_text(
        t(
            "status_line",
            lang=lang,
            emoji=_fmt_status_emoji(task.status),
            status=task.status,
            task_id=task.id,
            task=task.task_text[:200],
            iteration=task.iteration,
            summary=task.result_summary or "—",
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_logs(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args:
        tid = args[0]
    else:
        latest = await store.latest_task()
        if not latest:
            await update.effective_message.reply_text(t("no_tasks", lang=lang))
            return
        tid = latest.id
    entries = await store.tail_logs(tid, n=50)
    if not entries:
        await update.effective_message.reply_text(t("no_logs", lang=lang))
        return
    text = "\n".join(
        f"{datetime.fromtimestamp(e.ts):%H:%M:%S} [{e.level}] {e.message}" for e in entries
    )
    await _send(update.effective_message, text, code=True)


async def cmd_cancel(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    args = ctx.args or []
    target_id: str | None = args[0] if args else None
    if target_id is None:
        chat_id = update.effective_chat.id
        if chat_id not in _INFLIGHT:
            await update.effective_message.reply_text(t("no_inflight", lang=lang))
            return
        target_id, _, ev = _INFLIGHT[chat_id]
        ev.set()
        await update.effective_message.reply_text(
            t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
        )
        return
    for cid, (tid, _task, ev) in list(_INFLIGHT.items()):
        if tid == target_id:
            ev.set()
            await update.effective_message.reply_text(
                t("cancel_sent", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
            )
            return
    await update.effective_message.reply_text(
        t("cancel_not_running", lang=lang, task_id=target_id), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_history(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    tasks = await store.list_tasks(limit=10)
    if not tasks:
        await update.effective_message.reply_text(t("no_tasks", lang=lang))
        return
    lines = [
        f"{_fmt_status_emoji(task.status)} `{task.id}` i={task.iteration} {task.task_text[:80]}"
        for task in tasks
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_repo(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id
    args = ctx.args or []
    if not args:
        sess = await store.get_chat_session(chat_id)
        path = sess.get("repo_path") if sess else None
        await update.effective_message.reply_text(
            t("repo.current", lang=lang, path=path or "—"), parse_mode=ParseMode.MARKDOWN
        )
        return
    if args[0].lower() in ("clear", "none", "off"):
        await store.set_chat_repo(chat_id, None)
        await update.effective_message.reply_text(t("repo.cleared", lang=lang))
        return
    p = Path(args[0]).expanduser().resolve()
    if not p.exists():
        await update.effective_message.reply_text(t("repo.not_found", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN)
        return
    await store.set_chat_repo(chat_id, str(p))
    await update.effective_message.reply_text(t("repo.set", lang=lang, path=p), parse_mode=ParseMode.MARKDOWN)


async def cmd_resume(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(t("resume.usage", lang=lang))
        return
    store: Store = ctx.application.bot_data["store"]
    task = await store.get_task(args[0])
    if not task:
        await update.effective_message.reply_text(
            t("task_not_found", lang=lang, task_id=args[0]), parse_mode=ParseMode.MARKDOWN
        )
        return
    await _run_task_for_chat(update, ctx, task.task_text, resume_task_id=task.id)


async def cmd_exec(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    if not ctx.args:
        await update.effective_message.reply_text(t("exec.usage", lang=lang))
        return
    cmd = " ".join(ctx.args)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await _send(update.effective_message, t("exec.timeout", lang=lang), code=True)
            return
        out = out_b.decode("utf-8", errors="replace")
        suffix = f"\n[exit {proc.returncode}]"
        await _send(update.effective_message, (out or t("exec.no_output", lang=lang)) + suffix, code=True)
    except Exception as e:
        await _send(update.effective_message, f"error: {e}", code=True)


async def cmd_git(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    if not ctx.args or len(ctx.args) < 2:
        await update.effective_message.reply_text(t("git.usage", lang=lang))
        return
    repo = Path(ctx.args[0]).expanduser().resolve()
    sub = ctx.args[1]
    if sub not in GIT_WHITELIST:
        await update.effective_message.reply_text(
            t("git.not_whitelisted", lang=lang, sub=sub, whitelist=sorted(GIT_WHITELIST)),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not (repo / ".git").exists():
        await update.effective_message.reply_text(
            t("git.not_a_repo", lang=lang, path=repo), parse_mode=ParseMode.MARKDOWN
        )
        return
    rest = ctx.args[2:]
    cmd = ["git", "-C", str(repo), sub, *rest]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        out = out_b.decode("utf-8", errors="replace")
        await _send(update.effective_message, (out or "(no output)") + f"\n[exit {proc.returncode}]", code=True)
    except Exception as e:
        await _send(update.effective_message, f"error: {e}", code=True)


# ----- message handlers -----


async def on_text(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    await _run_task_for_chat(update, ctx, text)


async def on_voice(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    s = settings()
    if not s.openai_api_key:
        await update.effective_message.reply_text(t("voice.no_key", lang=lang))
        return
    msg = update.effective_message
    voice = msg.voice or msg.audio
    file = await ctx.bot.get_file(voice.file_id)
    target = s.workspaces_dir / "_voice" / f"{voice.file_unique_id}.ogg"
    target.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(str(target))

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=s.openai_api_key)
    with open(target, "rb") as f:
        transcript = await client.audio.transcriptions.create(model="whisper-1", file=f)
    text = (transcript.text or "").strip()
    if not text:
        await msg.reply_text(t("voice.empty", lang=lang))
        return
    await msg.reply_text(t("voice.transcript", lang=lang, text=text[:300]), parse_mode=ParseMode.MARKDOWN)
    await _run_task_for_chat(update, ctx, text)


async def on_photo_or_document(update: Update, ctx) -> None:
    if not await _owner_only(update, ctx):
        return
    lang = _lang(update)
    s = settings()
    msg = update.effective_message
    caption = (msg.caption or "").strip()
    if not caption:
        await msg.reply_text(t("photo.no_caption", lang=lang))
        return

    attachments: list[Path] = []
    if msg.photo:
        photo = msg.photo[-1]  # largest
        f = await ctx.bot.get_file(photo.file_id)
        target = s.workspaces_dir / "_attachments" / f"{photo.file_unique_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        await f.download_to_drive(str(target))
        attachments.append(target)
    if msg.document:
        doc = msg.document
        f = await ctx.bot.get_file(doc.file_id)
        target = s.workspaces_dir / "_attachments" / (doc.file_name or f"{doc.file_unique_id}.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        await f.download_to_drive(str(target))
        attachments.append(target)

    await _run_task_for_chat(update, ctx, caption, attachments=attachments)


# ------------------------------------------------------------------ startup


async def post_init(application: Application) -> None:
    s = settings()
    store = await Store.open(s.cascade_db_path)
    application.bot_data["store"] = store

    # Auto-resume: any 'running' tasks must be from a previous bot life — mark and continue.
    interrupted = await store.mark_running_as_interrupted()
    if interrupted and s.telegram_owner_id:
        try:
            await application.bot.send_message(
                chat_id=s.telegram_owner_id,
                text=t(
                    "startup.interrupted",
                    lang=s.cascade_bot_lang,
                    n=len(interrupted),
                    ids=", ".join(f"`{i}`" for i in interrupted),
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning("could not notify owner of interrupted tasks: %s", e)


async def post_shutdown(application: Application) -> None:
    store: Store | None = application.bot_data.get("store")
    if store is not None:
        await store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    s = settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")
    if not s.telegram_owner_id:
        raise SystemExit("TELEGRAM_OWNER_ID not set in .env")

    app = (
        Application.builder()
        .token(s.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("repo", cmd_repo))
    app.add_handler(CommandHandler("exec", cmd_exec))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("lang", cmd_lang))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_photo_or_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Cascade bot starting; owner=%s", s.telegram_owner_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
