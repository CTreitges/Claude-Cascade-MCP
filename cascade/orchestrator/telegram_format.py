"""Phase H — Multiplexed Telegram-Stream Formatter.

Wandelt den (sub_task_name, HarnessEvent)-Stream des Orchestrators in
einen kompakten Status-Block der in eine einzelne Telegram-Live-Card
passt — pro Sub-Task eine Zeile mit Status-Emoji + aktuellem Tool +
Tokens/Cost-Counter.

Designziel:
  - Eine Telegram-Nachricht für den ganzen Orchestrator-Run, nicht eine
    pro Sub-Task (sonst zu viel Spam bei vielen Tasks)
  - Update-Frequenz throttle-bar (default 2s), damit Telegram-Rate-Limit
    nicht greift (30 msg/sec/bot)
  - Lesbar im Telegram-Mobile-View: monospace, kurze Zeilen

Beispiel-Output:
    🪓 Sub-Tasks (Batch 2/3, 47s)
    ├─ ✅ explore        4 tools • $0.03 • 18s
    ├─ ⚙️  docs-alpha     🔍 Grep • 3 tools • $0.04 • 12s
    ├─ ⚙️  docs-beta      📖 Read • 2 tools • $0.02 • 9s
    └─ ⏸  tests          waiting on docs-alpha, docs-beta
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cascade.harness.base import (
    AssistantTextEvent,
    DoneEvent,
    HarnessEvent,
    ToolResultEvent,
    ToolUseEvent,
)


_TOOL_EMOJI = {
    "Read": "📖",
    "Glob": "🔎",
    "Grep": "🔍",
    "Edit": "✏️",
    "Write": "💾",
    "Bash": "💻",
    "Task": "🪓",
    "TodoWrite": "📝",
    "WebFetch": "🌐",
    "WebSearch": "🔍",
}


_STATUS_EMOJI = {
    "pending": "⏸",
    "starting": "🟢",
    "running": "⚙️",
    "done": "✅",
    "failed": "❌",
    "blocked": "🚫",
    "skipped": "⏭",
}


@dataclass
class _SubTaskState:
    name: str
    status: str = "pending"
    started_at: Optional[float] = None
    last_tool: str = ""
    tool_count: int = 0
    cost_usd: float = 0.0
    branch: Optional[str] = None
    blocker: str = ""
    error: str = ""

    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at


class MultiplexedStreamFormatter:
    """Hält State pro Sub-Task und formatiert den kombinierten Telegram-Block.

    Wird typisch aus dem Bot-runner heraus instanziiert; Orchestrator-Callbacks
    mappen direkt auf .on_event() / .on_status_change() ; der Bot-Heartbeat
    ruft .render() periodisch.
    """

    def __init__(self, lang: str = "de") -> None:
        self.lang = lang
        self.subtasks: Dict[str, _SubTaskState] = {}
        self.run_started_at = time.monotonic()
        self.batches_total: Optional[int] = None
        self.batches_done: int = 0

    def register_subtasks(self, names: List[str], batches_total: Optional[int] = None) -> None:
        """Initialisiert die State-Liste in Plan-Reihenfolge."""
        for n in names:
            self.subtasks.setdefault(n, _SubTaskState(name=n))
        if batches_total is not None:
            self.batches_total = batches_total

    async def on_status_change(self, name: str, info: Dict[str, Any]) -> None:
        """Callback für Orchestrator on_subtask_status."""
        st = self.subtasks.setdefault(name, _SubTaskState(name=name))
        new_status = info.get("status")
        if new_status:
            st.status = new_status
            if new_status == "running" and st.started_at is None:
                st.started_at = time.monotonic()
        if "branch" in info:
            st.branch = info["branch"]
        if "cost" in info:
            st.cost_usd = float(info["cost"])
        if "error" in info:
            st.error = str(info["error"])[:120]
        if "blocker" in info:
            st.blocker = str(info["blocker"])

    async def on_event(self, name: str, ev: HarnessEvent) -> None:
        """Callback für Orchestrator on_event (HarnessEvent pro Sub-Task)."""
        st = self.subtasks.setdefault(name, _SubTaskState(name=name))
        if isinstance(ev, ToolUseEvent):
            st.last_tool = ev.name
            st.tool_count += 1
        elif isinstance(ev, ToolResultEvent):
            # Last-Tool-Display bleibt, aber wir könnten errors annotieren
            if ev.is_error:
                st.last_tool = f"{st.last_tool}❗"
        elif isinstance(ev, DoneEvent):
            if ev.cost_usd:
                st.cost_usd = ev.cost_usd
            if not ev.success and ev.error:
                st.error = ev.error[:120]

    def render(self, max_lines: int = 12) -> str:
        """Rendert den aktuellen State als Telegram-Markdown-Block.

        Auf max_lines begrenzt — bei vielen Sub-Tasks werden „running" /
        „done" priorisiert, "pending" am Ende abgeschnitten.
        """
        if not self.subtasks:
            return ""

        elapsed_total = time.monotonic() - self.run_started_at
        m, s = divmod(int(elapsed_total), 60)
        header_de = f"🪓 *Sub-Tasks*"
        header_en = f"🪓 *Sub-Tasks*"
        if self.batches_total:
            header_de += f" — Batch {self.batches_done}/{self.batches_total}"
            header_en += f" — batch {self.batches_done}/{self.batches_total}"
        header_de += f" • {m}:{s:02d}"
        header_en += f" • {m}:{s:02d}"
        header = header_de if self.lang == "de" else header_en

        # Sortier-Reihenfolge: running > done > failed > blocked > pending
        priority = {"running": 0, "starting": 1, "done": 2, "failed": 3, "blocked": 4, "skipped": 5, "pending": 6}
        items = sorted(
            self.subtasks.values(),
            key=lambda s: (priority.get(s.status, 9), s.name),
        )

        lines = [header]
        shown = 0
        truncated = 0
        for i, st in enumerate(items):
            if shown >= max_lines and st.status in ("pending",):
                truncated += 1
                continue
            shown += 1
            is_last_visible = (
                shown == max_lines
                or i == len(items) - 1
                or (truncated == 0 and items[i + 1].status == "pending" and shown >= max_lines)
            )
            connector = "└─" if is_last_visible else "├─"
            lines.append(self._render_line(st, connector))
        if truncated:
            lines.append(
                f"  …{truncated} weitere ausstehend" if self.lang == "de"
                else f"  …{truncated} more pending"
            )

        # Total-Cost-Zeile
        total_cost = sum(s.cost_usd for s in self.subtasks.values())
        if total_cost > 0:
            cost_line = (
                f"\n💰 Gesamt: ${total_cost:.4f}" if self.lang == "de"
                else f"\n💰 Total: ${total_cost:.4f}"
            )
            lines.append(cost_line)

        return "\n".join(lines)

    def _render_line(self, st: _SubTaskState, connector: str) -> str:
        emoji = _STATUS_EMOJI.get(st.status, "·")
        name = st.name
        # Pad name auf max 14 chars
        name_disp = name[:14].ljust(14)

        if st.status == "running":
            tool_emoji = _TOOL_EMOJI.get(st.last_tool, "🔧") if st.last_tool else ""
            tool_str = f"{tool_emoji} {st.last_tool}" if st.last_tool else "…"
            cost_part = f" • ${st.cost_usd:.4f}" if st.cost_usd else ""
            elapsed = int(st.elapsed_s())
            time_part = f" • {elapsed}s" if elapsed > 0 else ""
            tools_part = f" • {st.tool_count} tools" if st.tool_count else ""
            return f"{connector} {emoji}  {name_disp} {tool_str}{tools_part}{cost_part}{time_part}"

        if st.status == "done":
            elapsed = int(st.elapsed_s())
            return (
                f"{connector} {emoji}  {name_disp} "
                f"{st.tool_count} tools • ${st.cost_usd:.4f} • {elapsed}s"
            )

        if st.status == "failed":
            err_short = (st.error or "—")[:40]
            return f"{connector} {emoji}  {name_disp} fehlgeschlagen: {err_short}"

        if st.status == "blocked":
            block_msg = (
                "wartet auf upstream-failure" if self.lang == "de"
                else "blocked by upstream failure"
            )
            return f"{connector} {emoji}  {name_disp} {block_msg}"

        if st.status in ("starting", "pending"):
            label = (
                "wartet" if (self.lang == "de" and st.status == "pending") else
                "waiting" if st.status == "pending" else
                "startet…" if self.lang == "de" else
                "starting…"
            )
            return f"{connector} {emoji}  {name_disp} {label}"

        return f"{connector} {emoji}  {name_disp} {st.status}"
