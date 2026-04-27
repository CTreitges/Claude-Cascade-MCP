from __future__ import annotations

import asyncio
from pathlib import Path


from cascade.agents.planner import Plan, RepoHint
from cascade.repo_resolver import (
    discover_local_repos,
    repos_for_planner_prompt,
    resolve_repo,
)


def _mkrepo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    (p / ".git").mkdir(exist_ok=True)
    return p


def test_discover_picks_up_git_dirs(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    _mkrepo(fake_home / "projekte" / "alpha")
    _mkrepo(fake_home / "projekte" / "beta")
    _mkrepo(fake_home / "code" / "gamma")
    (fake_home / "projekte" / "no_git").mkdir(parents=True)
    found = {p.name for p in discover_local_repos()}
    assert {"alpha", "beta", "gamma"}.issubset(found)
    assert "no_git" not in found


def test_discover_skips_node_modules(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    _mkrepo(fake_home / "projekte" / "alpha")
    _mkrepo(fake_home / "projekte" / "alpha" / "node_modules" / "evil")
    found = [p.name for p in discover_local_repos()]
    assert "alpha" in found
    assert "evil" not in found


def test_repos_for_planner_prompt_renders_block() -> None:
    block = repos_for_planner_prompt([Path("/a"), Path("/b/c")], "task")
    assert "/a" in block and "/b/c" in block
    assert "Locally available git repos" in block


def test_repos_for_planner_prompt_empty() -> None:
    assert repos_for_planner_prompt([], "x") == ""


# ---- resolve_repo ----


async def test_resolve_local_existing(tmp_path: Path) -> None:
    repo = _mkrepo(tmp_path / "myrepo")
    hint = RepoHint(kind="local", path=str(repo))
    r = await resolve_repo(hint, workspaces_root=tmp_path / "ws", task_id="t")
    assert r.path == repo
    assert r.attached is True
    assert r.source == "local"


async def test_resolve_local_missing_no_url_falls_back(tmp_path: Path) -> None:
    hint = RepoHint(kind="local", path=str(tmp_path / "nope"))
    r = await resolve_repo(hint, workspaces_root=tmp_path / "ws", task_id="t")
    assert r.path is None
    assert r.attached is False
    assert r.source == "fallback"


async def test_resolve_fresh_default(tmp_path: Path) -> None:
    hint = RepoHint(kind="fresh")
    r = await resolve_repo(hint, workspaces_root=tmp_path / "ws", task_id="t")
    assert r.path is None
    assert r.source == "fresh"


async def test_resolve_none_hint(tmp_path: Path) -> None:
    r = await resolve_repo(None, workspaces_root=tmp_path / "ws", task_id="t")
    assert r.path is None
    assert r.source == "fresh"


async def test_resolve_clone_uses_local_git_url(tmp_path: Path) -> None:
    """Use a real local 'git clone' (file URL) so we don't need network."""
    src = _mkrepo(tmp_path / "src")
    # init a real git repo so clone has something to fetch
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(src), "init", "-q",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    (src / "hello.txt").write_text("hi")
    for cmd in (
        ["git", "-C", str(src), "config", "user.email", "t@t"],
        ["git", "-C", str(src), "config", "user.name", "t"],
        ["git", "-C", str(src), "add", "-A"],
        ["git", "-C", str(src), "commit", "-q", "-m", "init"],
    ):
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p.wait()

    hint = RepoHint(kind="clone", url=str(src))
    r = await resolve_repo(hint, workspaces_root=tmp_path / "ws", task_id="cln")
    assert r.path is not None
    assert r.attached is True
    assert r.source == "cloned"
    assert (r.path / "hello.txt").read_text() == "hi"


async def test_resolve_local_missing_with_clone_fallback(tmp_path: Path) -> None:
    src = _mkrepo(tmp_path / "src")
    for cmd in (
        ["git", "-C", str(src), "init", "-q"],
        ["git", "-C", str(src), "config", "user.email", "t@t"],
        ["git", "-C", str(src), "config", "user.name", "t"],
    ):
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p.wait()
    (src / "x").write_text("x")
    for cmd in (
        ["git", "-C", str(src), "add", "-A"],
        ["git", "-C", str(src), "commit", "-q", "-m", "init"],
    ):
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await p.wait()
    hint = RepoHint(kind="local", path=str(tmp_path / "missing"), url=str(src))
    r = await resolve_repo(hint, workspaces_root=tmp_path / "ws", task_id="mc")
    assert r.source == "cloned"
    assert r.path is not None
    assert (r.path / "x").read_text() == "x"


# ---- Plan schema with repo ----


def test_plan_default_repo_is_fresh() -> None:
    p = Plan(summary="x", steps=["s1"], files_to_touch=[], acceptance_criteria=[])
    assert p.repo.kind == "fresh"
    assert p.repo.path is None


def test_plan_accepts_repo_hint() -> None:
    p = Plan(
        summary="x",
        steps=[],
        files_to_touch=[],
        acceptance_criteria=[],
        repo={"kind": "local", "path": "/tmp/foo", "rationale": "user said so"},
    )
    assert p.repo.kind == "local"
    assert p.repo.path == "/tmp/foo"
    assert p.repo.rationale == "user said so"
