"""Free-form message handlers: text (with smart triage), voice, photo/document."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.store import Store
from cascade.triage import triage

from ..helpers import lang_for, owner_only
from ..runner import run_task_for_chat

log = logging.getLogger("cascade.bot.messages")


async def on_text(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    lang = lang_for(update)
    s = settings()

    store: Store = ctx.application.bot_data["store"]
    recent = await store.list_tasks(limit=3)
    context_lines = []
    for past in recent:
        files_hint = ""
        try:
            iters = await store.list_iterations(past.id)
            last = iters[-1] if iters else None
            if last and last.diff_excerpt:
                import re
                files = sorted({
                    m.group(1) for m in re.finditer(r"diff --git a/(\S+) ", last.diff_excerpt or "")
                })
                if files:
                    files_hint = f" files=[{', '.join(files[:6])}]"
        except Exception:
            pass
        context_lines.append(
            f"- task_id={past.id} status={past.status} iter={past.iteration} "
            f"workspace={past.workspace_path or '—'} summary={(past.result_summary or '—')[:120]} "
            f"task={(past.task_text or '')[:140]}{files_hint}"
        )
    context = "\n".join(context_lines) if context_lines else None

    sess_now = await store.get_chat_session(update.effective_chat.id) or {}
    if sess_now.get("triage_effort"):
        s = s.model_copy(update={"cascade_triage_effort": sess_now["triage_effort"]})

    try:
        verdict = await triage(text, lang=lang, s=s, context=context)
    except Exception as e:
        log.warning("triage crashed (%s) — treating as task", e)
        await run_task_for_chat(update, ctx, text)
        return

    if verdict.is_task:
        await run_task_for_chat(update, ctx, verdict.task or text)
    else:
        reply = verdict.reply or ("Ok." if lang == "de" else "Ok.")
        await update.effective_message.reply_text(reply)


async def on_voice(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    s = settings()
    if not s.openai_api_key:
        await update.effective_message.reply_text(
            "OPENAI_API_KEY nicht gesetzt — Voice-Transkription nicht möglich."
            if lang == "de"
            else "OPENAI_API_KEY not set — cannot transcribe voice."
        )
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
        await msg.reply_text("(leere Transkription)" if lang == "de" else "(empty transcription)")
        return
    await msg.reply_text(f"📝 _{text[:300]}_", parse_mode=ParseMode.MARKDOWN)
    await run_task_for_chat(update, ctx, text)


async def on_photo_or_document(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    s = settings()
    msg = update.effective_message
    caption = (msg.caption or "").strip()
    if not caption:
        await msg.reply_text(
            "Bitte eine Bildunterschrift hinzufügen, die beschreibt was zu tun ist."
            if lang == "de"
            else "Please add a caption describing what to do with this attachment."
        )
        return

    attachments: list[Path] = []
    if msg.photo:
        photo = msg.photo[-1]
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

    await run_task_for_chat(update, ctx, caption, attachments=attachments)
