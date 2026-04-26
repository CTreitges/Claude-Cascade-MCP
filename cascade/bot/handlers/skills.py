"""Skill commands: /skills /run /skillupgrade + the suggestion accept/reject callback."""

from __future__ import annotations

import json
import logging

from telegram import Update
from telegram.constants import ParseMode

from cascade.store import Store

from ..helpers import lang_for, owner_only
from ..runner import run_task_for_chat
from ..state import PENDING_SKILL

log = logging.getLogger("cascade.bot.handlers.skills")


async def cmd_skills(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if args and args[0] == "delete" and len(args) >= 2:
        ok = await store.delete_skill(args[1])
        if lang == "de":
            await update.effective_message.reply_text("✅ Gelöscht." if ok else "Skill nicht gefunden.")
        else:
            await update.effective_message.reply_text("✅ Deleted." if ok else "Skill not found.")
        return

    skills = await store.list_skills()
    if not skills:
        await update.effective_message.reply_text(
            "Keine Skills gespeichert. Sie entstehen automatisch nach erfolgreichen Runs."
            if lang == "de" else
            "No skills saved yet. They are auto-suggested after successful runs."
        )
        return
    lines = ["*Gespeicherte Skills:*" if lang == "de" else "*Saved skills:*"]
    for sk in skills:
        used = sk.get("usage_count", 0)
        lines.append(f"• `{sk['name']}` — {sk.get('description') or '—'} (× {used})")
    lines.append("")
    lines.append(
        "Aufruf: `/run <name> <args>` — Platzhalter werden mit den args ersetzt."
        if lang == "de" else
        "Usage: `/run <name> <args>` — placeholders are replaced with args."
    )
    lines.append(
        "Löschen: `/skills delete <name>`" if lang == "de" else
        "Delete: `/skills delete <name>`"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_run_skill(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(
            "Aufruf: /run <skill_name> <args …>" if lang == "de"
            else "Usage: /run <skill_name> <args …>"
        )
        return
    name = args[0]
    sk = await store.get_skill_by_name(name)
    if not sk:
        await update.effective_message.reply_text(
            f"Skill `{name}` nicht gefunden. /skills für Liste."
            if lang == "de" else f"Skill `{name}` not found. /skills for list.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    template = sk["task_template"]
    params = args[1:]
    text = template
    kv = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in params if "=" in p}
    rest = [p for p in params if "=" not in p]
    try:
        text = template.format(*rest, **kv)
    except (KeyError, IndexError):
        text = template + ("\n\n" + " ".join(params) if params else "")
    await store.increment_skill_usage(name)
    await run_task_for_chat(update, ctx, text)


async def on_skill_callback(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    chat_id = update.effective_chat.id

    if data.startswith("sk:y:"):
        name = data.split(":", 2)[2]
        sug = PENDING_SKILL.pop(chat_id, None)
        if not sug or sug.get("name") != name:
            await q.edit_message_text(
                "⚠ Vorschlag nicht mehr verfügbar." if lang == "de"
                else "⚠ Suggestion no longer available."
            )
            return
        try:
            await store.create_skill(
                name=sug["name"],
                description=sug.get("description"),
                task_template=sug["task_template"],
                rationale=sug.get("rationale"),
                source_task_ids=[sug.get("task_id")] if sug.get("task_id") else [],
            )
            if sug.get("task_id"):
                await store.mark_skill_suggestion_decided(sug["task_id"], "accepted")
            from cascade.memory import remember_decision
            await remember_decision(
                f"New skill saved: '{name}' — {sug.get('description') or ''}. "
                f"Template: {sug.get('task_template', '')[:200]}",
                importance="high", tags="cascade-bot-mcp,skill,user-accepted",
            )
            await q.edit_message_text(
                f"✅ Skill `{name}` gespeichert. Aufruf via `/run {name} <args>`."
                if lang == "de" else
                f"✅ Skill `{name}` saved. Use `/run {name} <args>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await q.edit_message_text(f"❌ {e}")
        return
    if data.startswith("sk:n:"):
        sug = PENDING_SKILL.pop(chat_id, None)
        if sug and sug.get("task_id"):
            await store.mark_skill_suggestion_decided(sug["task_id"], "rejected")
        await q.edit_message_text("Verworfen." if lang == "de" else "Discarded.")
        return


_SKILLUPGRADE_SYSTEM = """Du optimierst einen einzelnen Coding-Bot-Skill.
Eingabe: aktueller Skill (name, description, task_template, placeholders,
rationale) plus die letzten Tasks die diesen Skill ausgelöst haben (oder
nahe dran waren). Gib einen knappen Optimierungs-Vorschlag aus.

Gib JSON zurück, kein Markdown:
{
  "should_update": true | false,
  "new_description": "<knapp, 1 Satz>" | null,
  "new_task_template": "<verbessertes Template mit {Platzhaltern}>" | null,
  "new_rationale": "<warum diese Änderung>" | null,
  "questions_for_user": ["<konkrete Frage 1>", "<...2>"],
  "comment": "<1-2 Sätze warum (oder warum NICHT) optimiert>"
}

Optimiere nur wenn klar besser. `questions_for_user` nur dann nicht-leer
wenn ohne User-Input keine sichere Entscheidung möglich ist (z.B. bei
mehrdeutigen Platzhaltern). Kein Padding, keine Floskeln."""


async def cmd_skillupgrade(update: Update, ctx) -> None:
    """Walk every saved skill, ask Opus whether it could be improved
    based on recent tasks, and optionally ask the user follow-up
    questions before applying the patch.

    Per-skill flow:
      1. Pull recent tasks (last 30 done) — pass them as context.
      2. Opus returns {should_update, new_description, new_task_template,
         new_rationale, questions_for_user, comment}.
      3. If `should_update=false` → log + skip.
      4. If `questions_for_user` non-empty → ask via `feedback.ask_user`,
         then re-prompt Opus with the answers.
      5. Apply via `store.update_skill`.
    """
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    store: Store = ctx.application.bot_data["store"]
    msg = update.effective_message
    chat_id = update.effective_chat.id

    skills = await store.list_skills()
    if not skills:
        await msg.reply_text(
            "Keine Skills zum Optimieren da."
            if lang == "de" else "No skills to upgrade.",
        )
        return

    from cascade.config import settings as _settings
    from cascade.feedback import ask_user
    from cascade.llm_client import LLMClientError, agent_chat
    from cascade.claude_cli import parse_json_payload

    s = _settings()
    recent_tasks = await store.list_tasks(limit=30, status="done")

    await msg.reply_text(
        f"🔧 Starte Skill-Optimierung für {len(skills)} Skill(s) — "
        f"das kann einen Moment dauern (Opus pro Skill, ggf. Rückfragen)."
        if lang == "de"
        else f"🔧 Starting skill upgrade for {len(skills)} skill(s) — this "
        f"will take a moment (Opus per skill, possibly with questions).",
    )

    summary: list[str] = []

    for sk in skills:
        skill_name = sk["name"]
        # Build prompt
        ctx_block = (
            f"CURRENT SKILL:\n  name: {skill_name}\n"
            f"  description: {sk.get('description') or '—'}\n"
            f"  task_template: {sk.get('task_template') or '—'}\n"
            f"  rationale: {sk.get('rationale') or '—'}\n"
            f"  usage_count: {sk.get('usage_count', 0)}\n\n"
            "RECENT DONE TASKS (most recent first, up to 30):\n"
        )
        for t in recent_tasks[:30]:
            ctx_block += f"  - {t.id} | {(t.task_text or '')[:200]}\n"

        try:
            raw = await agent_chat(
                prompt=ctx_block,
                model=s.cascade_planner_model,  # use planner (Opus default)
                system_prompt=_SKILLUPGRADE_SYSTEM,
                output_json=True,
                effort=s.cascade_planner_effort or None,
                timeout_s=180,
                # UX-tight retry budget — user is waiting in real-time.
                retry_max_total_wait_s=300.0,
                retry_min_backoff_s=15.0,
                s=s,
            )
            data = parse_json_payload(raw)
        except LLMClientError as e:
            await msg.reply_text(
                f"⚠️ `{skill_name}` — LLM-Fehler: {e}"
                if lang == "de" else f"⚠️ `{skill_name}` — LLM error: {e}",
                parse_mode=ParseMode.MARKDOWN,
            )
            summary.append(f"⚠️ `{skill_name}` — LLM-Fehler")
            continue
        except Exception as e:
            log.warning("skillupgrade parse failed for %s: %s", skill_name, e)
            summary.append(f"⚠️ `{skill_name}` — Parse-Fehler")
            continue

        if not data.get("should_update"):
            comment = (data.get("comment") or "").strip()
            summary.append(
                f"⏸ `{skill_name}` — keine Änderung ({comment[:80] or 'OK'})"
                if lang == "de"
                else f"⏸ `{skill_name}` — unchanged ({comment[:80] or 'OK'})"
            )
            continue

        questions = data.get("questions_for_user") or []
        if questions:
            qa_block = (
                f"❓ Fragen zum Skill `{skill_name}`:\n"
                + "\n".join(f"  • {q}" for q in questions[:4])
                + "\n\nBitte einen kombinierten Antworttext schicken."
            ) if lang == "de" else (
                f"❓ Questions for skill `{skill_name}`:\n"
                + "\n".join(f"  • {q}" for q in questions[:4])
                + "\n\nReply with a combined answer."
            )
            await msg.reply_text(qa_block, parse_mode=ParseMode.MARKDOWN)
            answer = (await ask_user(
                store, chat_id,
                f"upgrade-questions:{skill_name}",
                timeout_s=10 * 60, fallback="(no answer)",
            )).strip()
            # Re-ask Opus with the answers folded in.
            ctx2 = ctx_block + (
                "\n\nUSER ANSWERED THE FOLLOW-UP QUESTIONS WITH:\n"
                + answer + "\n\nNow produce the FINAL JSON decision."
            )
            try:
                raw2 = await agent_chat(
                    prompt=ctx2,
                    model=s.cascade_planner_model,
                    system_prompt=_SKILLUPGRADE_SYSTEM,
                    output_json=True,
                    effort=s.cascade_planner_effort or None,
                    timeout_s=180,
                    retry_max_total_wait_s=300.0,
                    retry_min_backoff_s=15.0,
                    s=s,
                )
                data = parse_json_payload(raw2)
            except Exception as e:
                summary.append(f"⚠️ `{skill_name}` — Re-Prompt-Fehler: {e}")
                continue

        if not data.get("should_update"):
            summary.append(
                f"⏸ `{skill_name}` — keine Änderung nach Antwort"
                if lang == "de" else f"⏸ `{skill_name}` — no change after answer"
            )
            continue

        ok = await store.update_skill(
            skill_name,
            description=data.get("new_description") or None,
            task_template=data.get("new_task_template") or None,
            rationale=data.get("new_rationale") or None,
        )
        if ok:
            new_tpl = (data.get("new_task_template") or "")[:160]
            summary.append(
                f"✅ `{skill_name}` aktualisiert\n   → {new_tpl}"
                if lang == "de"
                else f"✅ `{skill_name}` updated\n   → {new_tpl}"
            )
        else:
            summary.append(f"⚠️ `{skill_name}` — Update fehlgeschlagen")

    await msg.reply_text(
        ("*Ergebnis Skill-Upgrade:*\n" if lang == "de" else "*Skill upgrade result:*\n")
        + "\n".join(summary),
        parse_mode=ParseMode.MARKDOWN,
    )
    # Avoid pyflakes "unused json" warning by referencing the module — used
    # transitively by parse_json_payload.
    _ = json
