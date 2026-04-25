"""System commands: /exec /git /projects."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode

from cascade.config import settings
from cascade.repo_resolver import discover_local_repos

from ..helpers import lang_for, owner_only, send
from ..state import GIT_WHITELIST


async def cmd_exec(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    if not ctx.args:
        await update.effective_message.reply_text(
            "Aufruf: /exec <cmd …>" if lang == "de" else "Usage: /exec <cmd …>"
        )
        return
    cmd = " ".join(ctx.args)
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await send(
                update.effective_message,
                "⏱ Timeout nach 60s" if lang == "de" else "⏱ timed out after 60s",
                code=True,
            )
            return
        out = out_b.decode("utf-8", errors="replace")
        suffix = f"\n[exit {proc.returncode}]"
        none_msg = "(keine Ausgabe)" if lang == "de" else "(no output)"
        await send(update.effective_message, (out or none_msg) + suffix, code=True)
    except Exception as e:
        await send(update.effective_message, f"error: {e}", code=True)


async def cmd_git(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    if not ctx.args or len(ctx.args) < 2:
        await update.effective_message.reply_text(
            "Aufruf: /git <repo> <subcmd …>" if lang == "de"
            else "Usage: /git <repo> <subcmd …>"
        )
        return
    repo = Path(ctx.args[0]).expanduser().resolve()
    sub = ctx.args[1]
    if sub not in GIT_WHITELIST:
        await update.effective_message.reply_text(
            f"git subcommand `{sub}` nicht erlaubt. Whitelist: {sorted(GIT_WHITELIST)}"
            if lang == "de"
            else f"git subcommand `{sub}` not in whitelist: {sorted(GIT_WHITELIST)}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not (repo / ".git").exists():
        await update.effective_message.reply_text(
            f"Kein Git-Repo: `{repo}`" if lang == "de" else f"Not a git repo: `{repo}`",
            parse_mode=ParseMode.MARKDOWN,
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
        none_msg = "(keine Ausgabe)" if lang == "de" else "(no output)"
        await send(
            update.effective_message,
            (out or none_msg) + f"\n[exit {proc.returncode}]",
            code=True,
        )
    except Exception as e:
        await send(update.effective_message, f"error: {e}", code=True)


async def cmd_projects(update: Update, ctx) -> None:
    if not await owner_only(update, ctx):
        return
    lang = lang_for(update)
    args = ctx.args or []

    if args and args[0] == "delete" and len(args) >= 2:
        target = Path(args[1]).expanduser().resolve()
        home = Path.home()
        allowed_roots = [
            home / "projekte", home / "repos", home / "code", home / "dev",
            home / "claude-cascade" / "workspaces",
            Path("/tmp"),
        ]
        ok = any(target.is_relative_to(r) for r in allowed_roots if r.exists())
        if not ok:
            await update.effective_message.reply_text(
                f"⛔ Pfad nicht in erlaubten Wurzeln: `{target}`" if lang == "de"
                else f"⛔ Path outside allowed roots: `{target}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not target.exists():
            await update.effective_message.reply_text(
                f"❓ Pfad existiert nicht: `{target}`" if lang == "de"
                else f"❓ Path does not exist: `{target}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            shutil.rmtree(target)
            await update.effective_message.reply_text(
                f"🗑 Gelöscht: `{target}`" if lang == "de" else f"🗑 Deleted: `{target}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")
        return

    repos = await asyncio.to_thread(discover_local_repos)
    home = Path.home()

    s = settings()
    extras: list[Path] = []
    if s.workspaces_dir.exists():
        extras.extend(p for p in s.workspaces_dir.iterdir() if p.is_dir())
    for tmp_dir in Path("/tmp").glob("cascade-*"):
        if tmp_dir.is_dir():
            extras.append(tmp_dir)

    def _size_mb(p: Path) -> str:
        try:
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            return f"{total / 1024 / 1024:.1f}MB"
        except Exception:
            return "?"

    head = "📂 *Projekte & Workspaces*\n" if lang == "de" else "📂 *Projects & workspaces*\n"
    parts = [head]

    if repos:
        parts.append("\n*Git-Repos:*" if lang == "de" else "\n*Git repos:*")
        for r in repos[:30]:
            try:
                rel = r.relative_to(home)
                shown = f"~/{rel}"
            except ValueError:
                shown = str(r)
            parts.append(f"  • `{shown}` ({_size_mb(r)})")

    if extras:
        parts.append(
            "\n*Workspaces & /tmp:*" if lang == "de" else "\n*Workspaces & /tmp:*"
        )
        for p in sorted(extras)[:20]:
            parts.append(f"  • `{p}` ({_size_mb(p)})")

    parts.append(
        "\nLöschen mit: `/projects delete <pfad>`" if lang == "de"
        else "\nDelete with: `/projects delete <path>`"
    )
    parts.append(
        "(erlaubt nur ~/projekte, ~/repos, ~/code, ~/dev, ~/claude-cascade/workspaces, /tmp)"
    )

    text = "\n".join(parts)
    if len(text) > 3800:
        text = text[:3800] + "…"
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
