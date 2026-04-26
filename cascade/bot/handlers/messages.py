"""Free-form message handlers: text (with smart triage), voice, photo/document."""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from cascade.chat_memory import ChatMemory
from cascade.config import settings
from cascade.memory import recall_context, remember_fact
from cascade.store import Store
from cascade.triage import triage

from ..helpers import lang_for, owner_only
from ..runner import run_task_for_chat
from ..typing import TypingIndicator


def _classify_uploaded_json(text: str) -> dict | None:
    """If `text` looks like a recognizable JSON config/credential, return a
    classification dict {kind, summary, suggested_target, auto_stage_safe} —
    else None.

    `auto_stage_safe=True` means: the classification is unambiguous AND the
    suggested_target is a path the bot would always pick for this kind.
    The smart-document handler stages those WITHOUT asking the user.
    """
    import json
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return {
            "kind": "generic_json", "summary": "JSON data",
            "suggested_target": None, "auto_stage_safe": False,
        }

    if data.get("type") == "service_account" and "client_email" in data:
        project_id = data.get("project_id") or "unknown"
        return {
            "kind": "google_service_account",
            "summary": (
                f"Google Service-Account "
                f"({data.get('client_email', '?')}, "
                f"project={project_id})"
            ),
            "client_email": data.get("client_email"),
            "project_id": project_id,
            "suggested_target": f"~/.config/gcloud/{project_id}-sa.json",
            "auto_stage_safe": True,
        }
    if "installed" in data or "web" in data:
        sub = data.get("installed") or data.get("web") or {}
        if "client_id" in sub and "client_secret" in sub:
            return {
                "kind": "google_oauth_client",
                "summary": "Google OAuth client credentials",
                "suggested_target": "~/.config/gcloud/oauth-client.json",
                "auto_stage_safe": True,
            }
    # AWS credentials — sometimes shipped as JSON via `aws sts ...`
    if (
        "AccessKeyId" in data and "SecretAccessKey" in data
    ) or (
        "aws_access_key_id" in data and "aws_secret_access_key" in data
    ):
        return {
            "kind": "aws_credentials",
            "summary": "AWS access-key credentials",
            "suggested_target": "~/.aws/credentials.json",
            "auto_stage_safe": False,  # proper home is INI, not JSON — confirm with user
        }
    if "api_key" in data and isinstance(data.get("api_key"), str):
        return {
            "kind": "openai_credentials",
            "summary": "OpenAI-style {api_key: ...} credential file",
            "suggested_target": None,  # belongs in .env, not as file
            "auto_stage_safe": False,
        }
    return {
        "kind": "generic_config_json",
        "summary": f"JSON config with keys: {', '.join(list(data.keys())[:6])}",
        "suggested_target": None,
        "auto_stage_safe": False,
    }


def _classify_uploaded_text(name: str, text: str) -> dict | None:
    """Soft classification for non-JSON text uploads. Used to set
    `file_classification` on chat_messages so the recall layer has structure
    to grep on, even though we don't auto-stage these."""
    if not text:
        return None
    name_lower = (name or "").lower()
    head = text.lstrip()[:200]
    if name_lower.endswith((".md", ".markdown")):
        return {"kind": "markdown_doc", "summary": f"Markdown ({len(text)}B)"}
    if name_lower == "requirements.txt" or name_lower.endswith("/requirements.txt"):
        return {"kind": "requirements_txt", "summary": "Python requirements list"}
    if name_lower.endswith(".py") or head.startswith(("#!/usr/bin/env python", "import ", "from ")):
        return {"kind": "python_script", "summary": f"Python source ({len(text)}B)"}
    if name_lower.endswith(".env") or ".env." in name_lower:
        return {"kind": "dotenv_snippet", "summary": "dotenv KEY=VALUE list"}
    # Heuristic: lines look like KEY=VALUE
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and sum(1 for ln in lines if "=" in ln and not ln.lstrip().startswith("#")) >= max(1, len(lines) // 2):
        return {"kind": "dotenv_snippet", "summary": f"KEY=VALUE list ({len(lines)} lines)"}
    return None

log = logging.getLogger("cascade.bot.messages")


async def on_text(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    lang = lang_for(update)
    s = settings()
    chat_id_outer = update.effective_chat.id

    # Human-in-the-loop: if a cascade is currently waiting for an answer,
    # treat this incoming text as the reply, NOT as a new task. This
    # prevents the bot from spawning a parallel task while one is asking
    # for clarification.
    store: Store = ctx.application.bot_data["store"]
    pending = await store.get_pending_question(chat_id_outer)
    if pending:
        await store.answer_chat_question(pending["id"], text)
        await update.effective_message.reply_text(
            "📥 Antwort an die laufende Cascade weitergeleitet."
            if lang == "de"
            else "📥 Answer forwarded to the running cascade."
        )
        return

    async with TypingIndicator(ctx, chat_id_outer):
        await _on_text_impl(update, ctx, text, lang, s)


async def _on_text_impl(update, ctx, text, lang, s) -> None:
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
    # Always pin the most-recent successful task verbatim — covers things
    # like "use the project from earlier" / "the same workspace as just before".
    last_done = next((tk for tk in recent if tk.status == "done"), None)
    if last_done and last_done.task_text:
        context_lines.append(
            f"\nLAST_DONE_VERBATIM (task_id={last_done.id}, ws={last_done.workspace_path}):\n"
            f"  task: {(last_done.task_text or '')[:600]}\n"
            f"  result: {(last_done.result_summary or '—')[:300]}"
        )
    context = "\n".join(context_lines) if context_lines else None

    chat_id = update.effective_chat.id
    sess_now = await store.get_chat_session(chat_id) or {}
    if sess_now.get("triage_effort"):
        s = s.model_copy(update={"cascade_triage_effort": sess_now["triage_effort"]})

    # ChatMemory builds the full conversational context — USER FACTS,
    # RECENT UPLOADS (24h), CONVERSATION (last 30 with file content inline),
    # EARLIER (warm summaries), SEARCH HITS for `text`.
    chat_memory = ChatMemory(store)
    try:
        memory_block = await chat_memory.build_context(
            chat_id, query=text, lang=lang,
        )
    except Exception as e:
        log.warning("chat_memory.build_context failed: %s", e)
        memory_block = None

    # RLM recall (cross-session long-term findings) goes alongside the
    # chat-memory block — they're complementary, not overlapping.
    rlm_block: str | None = None
    try:
        rlm_block = await recall_context(text, limit=5)
    except Exception as e:
        log.debug("rlm recall failed: %s", e)
    if rlm_block:
        rlm_header = (
            "=== RLM-Memory (langfristig, BM25-Recall) ==="
            if lang == "de"
            else "=== RLM memory (long-term, BM25 recall) ==="
        )
        rlm_part = f"{rlm_header}\n{rlm_block}"
        memory_block = (
            f"{memory_block}\n\n{rlm_part}" if memory_block else rlm_part
        )

    # External research (Context7 + Brave) — auto-fetched bei Bedarf, damit
    # auch der Dispatcher up-to-date Library-Docs / Web-Treffer hat.
    if s.cascade_context7_enabled or s.cascade_websearch_enabled:
        try:
            from cascade.research import gather_external_context
            ext = await gather_external_context(
                text, lang=lang,
                enabled_context7=s.cascade_context7_enabled,
                enabled_websearch=s.cascade_websearch_enabled,
            )
        except Exception as e:
            log.debug("research gather failed: %s", e)
            ext = None
        if ext:
            context = f"{context}\n\n{ext}" if context else ext

    # Append the user's turn AFTER building memory (so the new line doesn't
    # echo into its own context).
    await chat_memory.append(chat_id, "user", text)
    # Persist the user's message into RLM so future recalls find it.
    await remember_fact(
        f"[chat {chat_id}] user: {text[:1500]}",
        importance="low",
        tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id}",
    )

    chat_model_choice = sess_now.get("chat_model") or None
    chat_temp_choice = sess_now.get("chat_temperature")
    try:
        verdict = await triage(
            text,
            lang=lang,
            s=s,
            context=context,            # recent tasks + external research
            memory_block=memory_block,   # ChatMemory + RLM
            model=chat_model_choice,
            temperature=chat_temp_choice,
        )
    except Exception as e:
        log.warning("triage crashed (%s) — treating as task", e)
        from cascade.error_log import log_error
        await log_error(
            "on_text.triage", e,
            chat_id=chat_id, model=chat_model_choice or s.cascade_triage_model,
            text_preview=text[:200],
        )
        await store.append_chat_message(chat_id, "bot", f"[task gestartet] {text[:200]}")
        await run_task_for_chat(update, ctx, text)
        return

    if verdict.is_task:
        dispatched = verdict.task or text

        # ---------- Direct-Action: skip cascade, run + quick-review ----------
        if verdict.direct_action:
            from cascade.simple_actions import run_action
            from cascade.quick_review import review_action
            from telegram.constants import ParseMode as _PM

            kind = verdict.direct_action.get("kind", "?")
            ds = verdict.direct_action.get("summary", "")
            await update.effective_message.reply_text(
                f"⚡ Direkte Aktion: *{kind}* — {ds}\n_(skip Cascade, mit Review)_"
                if lang == "de"
                else f"⚡ Direct action: *{kind}* — {ds}\n_(skipping cascade, will review)_",
                parse_mode=_PM.MARKDOWN,
            )
            res = await run_action(verdict.direct_action)
            log_block = "\n".join(f"  · {ln}" for ln in res.log) or "  (no log)"
            files_block = ", ".join(res.files_touched) or "—"
            if res.ok:
                review = await review_action(
                    user_request=text,
                    action_kind=res.kind,
                    action_summary=res.summary,
                    action_log=res.log,
                    files_touched=res.files_touched,
                    output=res.output,
                    lang=lang,
                    s=s,
                )
                review_marker = "✅" if review.passed else "⚠️"
                summary_text = (
                    f"{review_marker} *{res.kind}* — {res.summary}\n"
                    f"`files:` {files_block}\n"
                    f"`log:`\n{log_block}\n"
                    f"`reviewer:` _{(review.feedback or 'ok')[:200]}_"
                )
            else:
                summary_text = (
                    f"❌ *{res.kind}* fehlgeschlagen\n"
                    f"_{res.error or res.summary}_"
                )
            await update.effective_message.reply_text(
                summary_text, parse_mode=_PM.MARKDOWN,
            )
            await store.append_chat_message(
                chat_id, "bot",
                f"[direct-action {res.kind}] ok={res.ok} files={files_block[:120]}",
            )
            await remember_fact(
                f"[chat {chat_id}] direct-action {res.kind} ok={res.ok}: {res.summary[:300]}",
                importance="medium",
                tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id},direct-action,{res.kind}",
            )
            return

        await store.append_chat_message(chat_id, "bot", f"[task gestartet] {dispatched[:200]}")
        await remember_fact(
            f"[chat {chat_id}] task dispatched: {dispatched[:500]}",
            importance="medium",
            tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id},task-dispatch",
        )
        await run_task_for_chat(update, ctx, dispatched)
    else:
        reply = verdict.reply or ("Ok." if lang == "de" else "Ok.")
        await store.append_chat_message(chat_id, "bot", reply)
        await remember_fact(
            f"[chat {chat_id}] bot: {reply[:1500]}",
            importance="low",
            tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id}",
        )
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
    chat_id = update.effective_chat.id
    async with TypingIndicator(ctx, chat_id):
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
        store: Store = ctx.application.bot_data["store"]
        await store.append_chat_message(chat_id, "user", text)
        await store.append_chat_message(chat_id, "bot", f"[task gestartet] {text[:200]}")
        await remember_fact(
            f"[chat {chat_id}] voice→task: {text[:500]}",
            importance="medium",
            tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id},task-dispatch,voice",
        )
        await run_task_for_chat(update, ctx, text)


async def on_photo_or_document(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    s = settings()
    msg = update.effective_message
    caption = (msg.caption or "").strip()
    chat_id = update.effective_chat.id
    store: Store = ctx.application.bot_data["store"]

    async with TypingIndicator(ctx, chat_id):
        attachments: list[Path] = []
        doc_text_content: str | None = None
        doc_meta: dict = {}
        if msg.photo:
            photo = msg.photo[-1]
            f = await ctx.bot.get_file(photo.file_id)
            target = s.workspaces_dir / "_attachments" / f"{photo.file_unique_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            await f.download_to_drive(str(target))
            attachments.append(target)
            doc_meta["kind"] = "photo"
        if msg.document:
            doc = msg.document
            doc_meta = {
                "kind": "document",
                "name": doc.file_name or f"{doc.file_unique_id}.bin",
                "mime": doc.mime_type or "?",
                "size": doc.file_size or 0,
            }
            f = await ctx.bot.get_file(doc.file_id)
            target = s.workspaces_dir / "_attachments" / doc_meta["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            await f.download_to_drive(str(target))
            attachments.append(target)
            # If the file is small + text-ish, peek at its content so the
            # cascade can reason about it without having to open the file.
            try:
                if (doc_meta["size"] or 0) < 80_000:
                    raw = target.read_bytes()
                    txt = raw.decode("utf-8")
                    doc_text_content = txt[:50_000]
            except Exception:
                doc_text_content = None  # binary / encoding mismatch

        # ---------- JSON-Credentials Auto-Pre-Stage ----------
        # If the doc is a recognizable JSON credential / config, the bot
        # pre-stages it on disk (chmod 600) BEFORE dispatching the cascade.
        # The cascade then only has to wire it into env / config — it
        # doesn't need root-of-system filesystem access.
        #
        # Auto-stage rule (per user-decided plan): when the classification
        # is unambiguous AND the suggested target sits inside the simple-
        # actions allowlist, stage WITHOUT asking. Otherwise ask via the
        # human-in-the-loop helper with a generous 30-min timeout.
        json_class: dict | None = None
        staged_path: Path | None = None
        if (
            msg.document
            and (doc_meta.get("name", "").endswith(".json")
                 or doc_meta.get("mime", "").startswith("application/json"))
            and doc_text_content
        ):
            json_class = _classify_uploaded_json(doc_text_content)
            if json_class and json_class.get("suggested_target"):
                proposed = Path(json_class["suggested_target"]).expanduser()
                from cascade.simple_actions import is_target_in_allowlist

                proposed_str = str(proposed)
                auto_safe = bool(json_class.get("auto_stage_safe"))
                in_allowlist = is_target_in_allowlist(proposed_str)

                if auto_safe and in_allowlist:
                    # AUTO-STAGE — no question, just do it.
                    answer = "ok"
                    from telegram.constants import ParseMode as _PM
                    await msg.reply_text(
                        f"🤖 Erkannt: *{json_class['summary']}* — lege automatisch "
                        f"unter `{proposed}` ab (chmod 600)."
                        if lang == "de"
                        else f"🤖 Detected: *{json_class['summary']}* — auto-staging "
                        f"to `{proposed}` (chmod 600).",
                        parse_mode=_PM.MARKDOWN,
                    )
                else:
                    # Not auto-safe (or path outside allowlist) → ask.
                    from cascade.feedback import ask_user
                    question = (
                        f"🔐 Erkannt: *{json_class['summary']}*\n\n"
                        f"Vorschlag: ablegen unter `{proposed}` mit chmod 600.\n"
                        "Antworte mit:\n"
                        "  • `ok` (oder `ja`) — Vorschlag übernehmen\n"
                        "  • einem alternativen Pfad (z.B. `/home/chris/keys/sa.json`)\n"
                        "  • `cascade` — der Cascade überlassen (kein Pre-Stage)\n"
                        "  • `nein` — abbrechen"
                    ) if lang == "de" else (
                        f"🔐 Detected: *{json_class['summary']}*\n\n"
                        f"Proposed location: `{proposed}` with chmod 600.\n"
                        "Reply with:\n"
                        "  • `ok` — accept proposal\n"
                        "  • a custom absolute path\n"
                        "  • `cascade` — leave it to the cascade\n"
                        "  • `no` — abort"
                    )
                    from telegram.constants import ParseMode as _PM
                    await msg.reply_text(question, parse_mode=_PM.MARKDOWN)
                    # 30-min timeout: file uploads happen in real life with
                    # context-switches (look up project_id, paste path).
                    answer = (await ask_user(
                        store, chat_id,
                        json_class["summary"],
                        timeout_s=30 * 60, fallback="cascade",
                    )).strip().lower()
                if answer in ("nein", "no", "n", "abort"):
                    await msg.reply_text(
                        "🚫 Abgebrochen — Datei bleibt unter "
                        f"`{attachments[0]}`."
                        if lang == "de"
                        else f"🚫 Aborted — file still at `{attachments[0]}`.",
                        parse_mode=_PM.MARKDOWN,
                    )
                    return
                if answer not in ("ok", "ja", "yes", "y", "j", "cascade"):
                    # Treat as alt path
                    try:
                        proposed = Path(answer).expanduser()
                    except Exception:
                        proposed = Path(json_class["suggested_target"]).expanduser()
                if answer != "cascade":
                    try:
                        import shutil
                        proposed.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            proposed.parent.chmod(0o700)
                        except Exception:
                            pass
                        shutil.copyfile(attachments[0], proposed)
                        proposed.chmod(0o600)
                        staged_path = proposed
                        await msg.reply_text(
                            f"✅ Datei abgelegt: `{proposed}` (chmod 600)"
                            if lang == "de"
                            else f"✅ File staged: `{proposed}` (chmod 600)",
                            parse_mode=_PM.MARKDOWN,
                        )
                        await remember_fact(
                            f"[chat {chat_id}] credential staged: "
                            f"{json_class['kind']} → {proposed} ({json_class['summary']})",
                            importance="high",
                            tags=f"cascade-bot-mcp,credential,chat-{chat_id}",
                        )
                        # Persist as ground-truth user fact so future
                        # cascades see it without having to recall.
                        try:
                            await store.set_user_fact(
                                chat_id, f"credential.{json_class['kind']}.path",
                                str(proposed),
                            )
                            if json_class.get("client_email"):
                                await store.set_user_fact(
                                    chat_id, f"credential.{json_class['kind']}.client_email",
                                    json_class["client_email"],
                                )
                        except Exception:
                            pass
                    except Exception as e:
                        await msg.reply_text(
                            f"⚠️ Konnte Datei nicht ablegen: {e}\nFalle zurück auf Cascade-Pfad."
                            if lang == "de"
                            else f"⚠️ Could not stage file: {e}\nFalling back to cascade path."
                        )

        # ---------- Smart-context build (works with OR without caption) ----------
        # Always pull the immediate prior chat context so the cascade has
        # explicit anchors for "this", "for the project from earlier",
        # "the json I just talked about".
        history = await store.recent_chat_messages(chat_id, limit=10)
        last_user = [m["text"] for m in history if m.get("role") == "user"][-3:]
        last_bot = [m["text"] for m in history if m.get("role") == "bot"][-2:]
        recent_tasks = await store.list_tasks(limit=2)
        recent_task_block = ""
        for tk in recent_tasks:
            recent_task_block += (
                f"- task `{tk.id}` status=`{tk.status}` workspace=`{tk.workspace_path or '—'}`\n"
                f"  text: {(tk.task_text or '')[:240]}\n"
                f"  summary: {(tk.result_summary or '—')[:200]}\n"
            )

        intro_de = (
            "Eine Datei wurde mitgeschickt — bestimme aus dem Kontext, was zu "
            "tun ist (z.B. an die richtige Stelle ablegen, Env-Vars setzen, "
            "in ein bestehendes Projekt integrieren) und führe das Setup aus."
        )
        intro_en = (
            "A file was attached — figure out from context what to do "
            "(e.g. drop it in the right place, set env vars, integrate "
            "into an existing project) and perform the setup."
        )
        intro = intro_de if lang == "de" else intro_en

        # Dateiinfo-Block
        doc_block = ""
        if doc_meta.get("kind") == "document":
            doc_block = (
                f"\n--- ANGEHÄNGTE DATEI ---\n"
                f"name: `{doc_meta['name']}`\n"
                f"mime: `{doc_meta['mime']}`\n"
                f"size: {doc_meta['size']} bytes\n"
                f"local-path: `{attachments[0]}`\n"
            )
            if doc_text_content is not None:
                doc_block += f"\nINHALT (erste 50 KB):\n```\n{doc_text_content}\n```\n"
            else:
                doc_block += "\n(binärer / nicht-textueller Inhalt)\n"

        ctx_block = ""
        if last_user or last_bot:
            ctx_block += "\n--- LETZTE CHAT-NACHRICHTEN ---\n"
            for txt in last_user:
                ctx_block += f"User: {txt[:300]}\n"
            for txt in last_bot:
                ctx_block += f"Bot:  {txt[:300]}\n"
        if recent_task_block:
            ctx_block += f"\n--- LETZTE TASKS ---\n{recent_task_block}"

        # Persistente User-Facts — projekt-bezogene ground truth aus
        # vorherigen Sessions (z.B. wo das soundcloud-Plugin liegt).
        try:
            facts = await store.get_user_facts(chat_id)
        except Exception:
            facts = {}
        if facts:
            ctx_block += "\n--- PERSISTENTE USER-FACTS ---\n"
            for k, v in list(facts.items())[:30]:
                ctx_block += f"  {k} = {v[:200]}\n"

        # Staging-Info: wenn Bot die Datei schon platziert hat, sag dem
        # Cascade-Planner explizit dass es nicht mehr selbst kopieren muss.
        staging_block = ""
        if staged_path is not None and json_class is not None:
            staging_block = (
                f"\n--- BOT HAT BEREITS GETAN ---\n"
                f"Die Credential-Datei wurde vom Bot platziert:\n"
                f"  Quelle (Telegram-Upload): `{attachments[0]}`\n"
                f"  Ziel (gestaged): `{staged_path}` (chmod 600)\n"
                f"  Klassifikation: {json_class.get('summary', '?')}\n"
                f"\nDeine Aufgabe: NICHT erneut kopieren. Stattdessen die "
                f"`.env`-Datei des passenden Projekts (sieh letzte Tasks/Chat) "
                f"so anpassen, dass die ENV-Variable auf `{staged_path}` zeigt. "
                f"Dann den smoke-Test ausführen falls vorhanden, und dem User "
                f"klare Setup-Bestätigung geben."
            )

        if caption:
            task_text = caption + doc_block + staging_block + ctx_block
        else:
            task_text = intro + doc_block + staging_block + ctx_block + (
                "\n\nWenn der Kontext mehrdeutig ist, frage zurück (cascade ask_user) "
                "statt blind zu raten."
                if lang == "de"
                else "\n\nIf the context is ambiguous, ask back (cascade ask_user) "
                "rather than guess blindly."
            )

        # Persist the file-arrival event into chat-memory + RLM so future
        # recalls find it ("the JSON I sent you yesterday"). The new
        # ChatMemory.append() inlines the file content (up to 30KB) into
        # chat_messages — that is what fixes the Drive-Setup amnesia: the
        # next "do you remember the json?" message sees the actual content
        # in CONVERSATION (Hot tier), not just a "[file received]" marker.
        marker = (
            f"[file received] {doc_meta.get('name', 'photo')} "
            f"({doc_meta.get('mime', 'image')}, {doc_meta.get('size', '?')} bytes)"
        )
        # Build a classification dict for non-JSON uploads too.
        classification = json_class
        if classification is None and msg.document:
            classification = _classify_uploaded_text(
                doc_meta.get("name", ""), doc_text_content or "",
            )
        chat_memory_local = ChatMemory(store)
        await chat_memory_local.append(
            chat_id, "user", marker,
            file_path=str(staged_path) if staged_path else (
                str(attachments[0]) if attachments else None
            ),
            file_content=doc_text_content,
            file_classification=classification,
        )
        await remember_fact(
            f"[chat {chat_id}] received file {doc_meta.get('name', 'photo')} "
            f"({doc_meta.get('mime', '?')}); auto-task dispatched.",
            importance="medium",
            tags=f"cascade-bot-mcp,telegram-chat,chat-{chat_id},file-upload",
        )

        await run_task_for_chat(update, ctx, task_text, attachments=attachments)
