"""Smoke-Test für Phase H — MultiplexedStreamFormatter.

Simuliert einen Orchestrator-Run synthetisch (ohne echte LLM-Calls)
und überprüft das gerenderte Telegram-Layout."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from cascade.harness.base import (
    AssistantTextEvent,
    DoneEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from cascade.orchestrator.telegram_format import MultiplexedStreamFormatter


def passed(label):
    print(f"  ✅ {label}")


def failed(label, why):
    print(f"  ❌ {label}: {why}")
    raise SystemExit(1)


async def test_basic_lifecycle():
    print("\n[1] basic lifecycle: starting → running → done")
    fmt = MultiplexedStreamFormatter(lang="de")
    fmt.register_subtasks(["explore", "fix-a", "fix-b"], batches_total=2)
    await fmt.on_status_change("explore", {"status": "starting"})
    await fmt.on_status_change("explore", {"status": "running"})
    await fmt.on_event("explore", ToolUseEvent(name="Glob", args={"pattern": "**/*.py"}))
    await fmt.on_event("explore", ToolUseEvent(name="Read", args={"file_path": "alpha.py"}))
    out = fmt.render()
    assert "explore" in out and "🔧" in out or "📖" in out or "🔎" in out
    assert "tools" in out
    assert "fix-a" in out and "fix-b" in out
    print(out)
    passed("running mit tool_count + tool emoji")

    await fmt.on_event("explore", DoneEvent(cost_usd=0.04, success=True))
    await fmt.on_status_change("explore", {"status": "done", "cost": 0.04})
    out2 = fmt.render()
    assert "✅" in out2 and "0.0400" in out2
    print(out2)
    passed("done mit final cost + tools")


async def test_multiple_running_parallel():
    print("\n[2] parallel running mit unterschiedlichen Tools")
    fmt = MultiplexedStreamFormatter(lang="de")
    fmt.register_subtasks(["a", "b", "c"], batches_total=2)
    await fmt.on_status_change("a", {"status": "running"})
    await fmt.on_status_change("b", {"status": "running"})
    await fmt.on_event("a", ToolUseEvent(name="Edit", args={}))
    await fmt.on_event("b", ToolUseEvent(name="Bash", args={}))
    out = fmt.render()
    assert "Edit" in out and "Bash" in out
    print(out)
    passed("zwei parallel running, beide mit unterschiedlichem Tool")


async def test_failed_blocks_dependent():
    print("\n[3] failed → blocked status")
    fmt = MultiplexedStreamFormatter(lang="de")
    fmt.register_subtasks(["a", "b", "c"])
    await fmt.on_status_change("a", {"status": "failed", "error": "TypeError: foo"})
    await fmt.on_status_change("b", {"status": "blocked"})
    out = fmt.render()
    assert "fehlgeschlagen" in out
    assert "wartet auf upstream-failure" in out
    print(out)
    passed("failed mit error-message, blocked mit upstream-Hinweis")


async def test_truncation():
    print("\n[4] truncation bei 12+ Sub-Tasks")
    fmt = MultiplexedStreamFormatter(lang="de")
    names = [f"task-{i:02d}" for i in range(15)]
    fmt.register_subtasks(names)
    out = fmt.render(max_lines=8)
    assert "weitere ausstehend" in out
    print(out)
    passed(f"truncation kicked in (15 → ~8 visible)")


async def test_cost_aggregation():
    print("\n[5] total cost aggregation")
    fmt = MultiplexedStreamFormatter(lang="de")
    fmt.register_subtasks(["a", "b"])
    await fmt.on_status_change("a", {"status": "done", "cost": 0.025})
    await fmt.on_status_change("b", {"status": "done", "cost": 0.075})
    out = fmt.render()
    assert "Gesamt: $0.1000" in out
    print(out)
    passed("total cost row korrekt aggregiert")


async def test_lang_en():
    print("\n[6] english lang")
    fmt = MultiplexedStreamFormatter(lang="en")
    fmt.register_subtasks(["a"])
    await fmt.on_status_change("a", {"status": "blocked"})
    out = fmt.render()
    assert "blocked by upstream failure" in out
    passed("EN translations active")


async def main():
    print("=" * 60)
    print("  MultiplexedStreamFormatter Smoke")
    print("=" * 60)
    await test_basic_lifecycle()
    await test_multiple_running_parallel()
    await test_failed_blocks_dependent()
    await test_truncation()
    await test_cost_aggregation()
    await test_lang_en()
    print("\n" + "=" * 60)
    print("  ✅ Alle 6 Tests grün")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
