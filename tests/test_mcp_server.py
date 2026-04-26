"""Tests for mcp_server tool surface — the tool registry and the sync=False
race-free path that was reordered in v0.3.0."""

from __future__ import annotations

import asyncio
from pathlib import Path


import mcp_server


def test_tool_registry_lists_all_tools():
    """The MCP server exposes the documented tool surface (now 10 tools)."""
    tools = list(mcp_server.mcp._tool_manager._tools.keys())
    expected = {
        "run_cascade_tool",
        "cascade_status",
        "cascade_logs",
        "cascade_cancel",
        "cascade_history",
        "cascade_summary",
        "cascade_resume",
        "cascade_dryrun",
        "cascade_skills_list",
        "cascade_skill_run",
    }
    assert expected == set(tools)


def test_tools_have_descriptions():
    """Every registered tool has a non-empty description (visible in Claude Code)."""
    for name, tool in mcp_server.mcp._tool_manager._tools.items():
        assert tool.description, f"tool {name} has no description"


# ---------- sync=False eager-DB path ----------


async def test_run_cascade_tool_sync_false_returns_id_immediately(monkeypatch, tmp_path: Path):
    """sync=False must:
    1. Create the DB row eagerly (so the caller gets a stable task_id).
    2. Launch *exactly one* background asyncio.Task with resume_task_id pointing
       at that row (no cancel-and-replace dance).
    """
    from cascade.config import Settings

    db_path = tmp_path / "mcp.db"
    s = Settings(
        cascade_db_path=db_path,
        cascade_home=tmp_path / "ws-home",
    )
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    runs = []
    finished = asyncio.Event()

    async def fake_run_cascade(*, resume_task_id=None, **kw):
        runs.append({"resume_task_id": resume_task_id, **kw})
        # Simulate a slow run so we can observe sync=False returning early.
        try:
            await asyncio.sleep(0.5)
        finally:
            finished.set()
        from cascade.core import CascadeResult
        return CascadeResult(
            task_id=resume_task_id or "x",
            status="done",
            iterations=1,
            plan=None,
            final_review=None,
            workspace_path=tmp_path,
            summary="ok",
        )

    monkeypatch.setattr(mcp_server, "run_cascade", fake_run_cascade)

    started = asyncio.get_event_loop().time()
    result = await mcp_server.run_cascade_tool(
        task="hello", repo=None, sync=False, timeout_s=30,
    )
    duration = asyncio.get_event_loop().time() - started

    # Must return well before the 0.5s simulated work is done.
    assert duration < 0.3
    assert result["status"] == "running"
    assert result["sync"] is False
    assert "task_id" in result
    tid = result["task_id"]
    assert tid in mcp_server._RUNNING

    # Wait for the background task to actually finish.
    await asyncio.wait_for(finished.wait(), timeout=5)
    # Give the done_callback a tick to run.
    await asyncio.sleep(0.05)

    # Exactly one run_cascade call (no race / no replacement).
    assert len(runs) == 1
    assert runs[0]["resume_task_id"] == tid

    # The task is removed from _RUNNING after completion.
    assert tid not in mcp_server._RUNNING

    # The DB row was eagerly created and is queryable.
    from cascade.store import Store
    store = await Store.open(db_path)
    try:
        t = await store.get_task(tid)
        assert t is not None
        assert t.task_text == "hello"
        assert t.source == "mcp"
    finally:
        await store.close()


async def test_run_cascade_tool_sync_true_returns_full_result(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    async def fake_run_cascade(**kw):
        from cascade.core import CascadeResult
        return CascadeResult(
            task_id="abc123", status="done", iterations=2, plan=None, final_review=None,
            workspace_path=tmp_path, summary="finished", diff="d",
        )

    monkeypatch.setattr(mcp_server, "run_cascade", fake_run_cascade)
    result = await mcp_server.run_cascade_tool(task="x", sync=True, timeout_s=10)

    assert result["task_id"] == "abc123"
    assert result["status"] == "done"
    assert result["iterations"] == 2
    assert result["summary"] == "finished"
    assert result["diff_chars"] == 1
    assert result["error"] is None


async def test_run_cascade_tool_sync_true_timeout(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    async def slow(*, cancel_event=None, **_kw):
        # Block longer than the timeout
        await asyncio.sleep(5)
        from cascade.core import CascadeResult
        return CascadeResult(
            task_id="x", status="done", iterations=1, plan=None, final_review=None,
            workspace_path=tmp_path, summary="",
        )

    monkeypatch.setattr(mcp_server, "run_cascade", slow)
    result = await mcp_server.run_cascade_tool(task="x", sync=True, timeout_s=1)
    assert result["status"] == "timeout"
    assert "1s" in result["summary"]


# ---------- cascade_status / cascade_logs / cascade_history ----------


async def test_cascade_status_not_found(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)
    result = await mcp_server.cascade_status("nope")
    assert result["error"] == "not found"


async def test_cascade_status_finds_task(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    from cascade.store import Store
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    store = await Store.open(s.cascade_db_path)
    tid = await store.create_task(source="mcp", task_text="from test")
    await store.update_task(tid, status="done", result_summary="ok", completed=True)
    await store.close()

    result = await mcp_server.cascade_status(tid)
    assert result["task_id"] == tid
    assert result["status"] == "done"


async def test_cascade_history_returns_recent(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    from cascade.store import Store
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    store = await Store.open(s.cascade_db_path)
    for i in range(3):
        await store.create_task(source="mcp", task_text=f"task {i}")
    await store.close()

    result = await mcp_server.cascade_history(limit=10)
    assert len(result) == 3
    assert all("task_id" in r for r in result)


async def test_cascade_cancel_unknown_task():
    result = await mcp_server.cascade_cancel("not-running")
    assert result["cancelled"] is False
    assert "not running" in result["reason"]


# ---------- skills tools ----------


async def test_cascade_skills_list_empty(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)
    skills = await mcp_server.cascade_skills_list()
    assert skills == []


async def test_cascade_skills_list_returns_saved(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    from cascade.store import Store
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)
    store = await Store.open(s.cascade_db_path)
    await store.create_skill(
        name="pytest_for", description="generate pytest tests",
        task_template="Erstelle pytest tests für {file}",
    )
    await store.close()
    skills = await mcp_server.cascade_skills_list()
    assert len(skills) == 1
    assert skills[0]["name"] == "pytest_for"
    assert "{file}" in skills[0]["task_template"]


async def test_cascade_skill_run_unknown(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)
    result = await mcp_server.cascade_skill_run("nope", args={})
    assert result["error"] == "skill not found"


async def test_cascade_skill_run_dispatches(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    from cascade.store import Store
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    store = await Store.open(s.cascade_db_path)
    await store.create_skill(
        name="echo_skill", description=None,
        task_template="echo {what}",
    )
    await store.close()

    captured = {}

    async def fake_run_cascade(**kw):
        captured["task"] = kw["task"]
        from cascade.core import CascadeResult
        return CascadeResult(
            task_id="r1", status="done", iterations=1, plan=None,
            final_review=None, workspace_path=tmp_path, summary="ok",
        )

    monkeypatch.setattr(mcp_server, "run_cascade", fake_run_cascade)
    result = await mcp_server.cascade_skill_run(
        "echo_skill", args={"what": "hello"}, sync=True, timeout_s=10
    )
    assert result["status"] == "done"
    assert captured["task"] == "echo hello"
    # Skill usage_count must have been incremented
    store = await Store.open(s.cascade_db_path)
    sk = await store.get_skill_by_name("echo_skill")
    await store.close()
    assert sk["usage_count"] == 1


async def test_cascade_dryrun_returns_plan(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    from cascade.agents.planner import Plan
    fake_plan = Plan(
        summary="dry-run summary",
        steps=["step1", "step2"],
        files_to_touch=["a.py"],
        acceptance_criteria=["c1"],
    )

    async def fake_planner(*args, **kw):
        return fake_plan

    import cascade.agents.planner as planner_mod
    monkeypatch.setattr(planner_mod, "call_planner", fake_planner)

    result = await mcp_server.cascade_dryrun("do thing")
    assert result["summary"] == "dry-run summary"
    assert result["steps"] == ["step1", "step2"]


async def test_cascade_resume_not_found(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)
    result = await mcp_server.cascade_resume("nope")
    assert result["error"] == "not found"


async def test_run_cascade_tool_passes_efforts_and_replan(monkeypatch, tmp_path: Path):
    from cascade.config import Settings
    s = Settings(cascade_db_path=tmp_path / "mcp.db", cascade_home=tmp_path / "ws-home")
    monkeypatch.setattr(mcp_server, "settings", lambda: s)

    captured = {}

    async def fake_run_cascade(**kw):
        captured.update(kw)
        from cascade.core import CascadeResult
        return CascadeResult(
            task_id="t", status="done", iterations=1, plan=None,
            final_review=None, workspace_path=tmp_path, summary="",
        )

    monkeypatch.setattr(mcp_server, "run_cascade", fake_run_cascade)
    await mcp_server.run_cascade_tool(
        task="x", sync=True,
        planner_effort="high",
        reviewer_effort="low",
        replan_max=3,
    )
    assert captured["planner_effort"] == "high"
    assert captured["reviewer_effort"] == "low"
    assert captured["replan_max"] == 3
