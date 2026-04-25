"""Tests for attached-mode behavior and quality-check execution."""

from __future__ import annotations

import subprocess
from pathlib import Path


from cascade.workspace import FileOp, QualityCheck, Workspace


def _git_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(p), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "t"], check=True)
    (p / "existing.txt").write_text("orig\n")
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)
    return p


def _git_head(p: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(p), "rev-parse", "HEAD"], text=True
    ).strip()


def _git_log_count(p: Path) -> int:
    return int(subprocess.check_output(
        ["git", "-C", str(p), "rev-list", "--count", "HEAD"], text=True
    ).strip())


# ---------- attached behavior ----------


def test_attach_records_base_ref(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    head = _git_head(repo)
    ws = Workspace.attach(repo)
    assert ws.is_attached is True
    assert ws.base_ref == head


def test_attached_commit_iteration_is_noop(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    before = _git_log_count(repo)
    ws = Workspace.attach(repo)
    ws.apply_ops([FileOp(op="write", path="new.txt", content="hi")])
    ws.commit_iteration(1)
    ws.commit_iteration(2)
    after = _git_log_count(repo)
    assert after == before, "attached repo must not gain iter commits"


def test_attached_diff_shows_new_file_against_base_ref(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "r")
    ws = Workspace.attach(repo)
    ws.apply_ops([FileOp(op="write", path="new.txt", content="hello\n")])
    diff = ws.diff()
    assert "new.txt" in diff
    assert "+hello" in diff


def test_detached_commit_iteration_creates_commit(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="d1")
    before = int(subprocess.check_output(
        ["git", "-C", str(ws.root), "rev-list", "--count", "HEAD"], text=True
    ).strip())
    ws.apply_ops([FileOp(op="write", path="x.txt", content="x")])
    ws.commit_iteration(1)
    after = int(subprocess.check_output(
        ["git", "-C", str(ws.root), "rev-list", "--count", "HEAD"], text=True
    ).strip())
    assert after == before + 1


def test_attached_diff_includes_new_and_modified(tmp_path: Path) -> None:
    """Both untracked Cascade files AND staged modifications should appear in
    the diff vs base_ref. Pre-existing user edits are also captured (acceptable
    trade-off — see workspace.diff() comment); the no-pollution guarantee is
    that we don't *commit* on the user's branch."""
    repo = _git_repo(tmp_path / "r")
    ws = Workspace.attach(repo)
    ws.apply_ops([FileOp(op="write", path="cascade.txt", content="hi\n")])
    diff = ws.diff()
    assert "cascade.txt" in diff
    assert "+hi" in diff


# ---------- quality checks ----------


async def test_run_check_success(tmp_path: Path) -> None:
    import sys
    ws = Workspace.create(tmp_path / "wb", task_id="c1")
    ws.apply_ops([FileOp(op="write", path="hello.py", content="print('hi')\n")])
    res = await ws.run_check(QualityCheck(
        name="syntax",
        command=f"{sys.executable} -c 'import ast; ast.parse(open(\"hello.py\").read())'",
        timeout_s=10,
    ))
    assert res.ok is True
    assert res.exit_code == 0


async def test_run_check_failure(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="c2")
    res = await ws.run_check(QualityCheck(
        name="missing-file", command="test -f does_not_exist", timeout_s=5
    ))
    assert res.ok is False
    assert res.exit_code != 0


async def test_run_check_timeout(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="c3")
    res = await ws.run_check(QualityCheck(
        name="slow", command="sleep 3", timeout_s=1
    ))
    assert res.ok is False
    assert "timeout" in res.output.lower()


async def test_run_check_expected_substring(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="c4")
    ok = await ws.run_check(QualityCheck(
        name="ok-sub", command="echo hello world", expected_substring="hello", timeout_s=5
    ))
    assert ok.ok is True
    miss = await ws.run_check(QualityCheck(
        name="miss-sub", command="echo hello world", expected_substring="ZZZ", timeout_s=5
    ))
    assert miss.ok is False


async def test_run_check_must_succeed_false(tmp_path: Path) -> None:
    """When must_succeed=False, a non-zero exit is still considered ok."""
    ws = Workspace.create(tmp_path / "wb", task_id="c5")
    res = await ws.run_check(QualityCheck(
        name="optional", command="false", must_succeed=False, timeout_s=5
    ))
    assert res.ok is True
    assert res.exit_code != 0


async def test_run_check_uses_workspace_cwd(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="c6")
    ws.apply_ops([FileOp(op="write", path="sentinel.txt", content="found\n")])
    res = await ws.run_check(QualityCheck(
        name="cwd", command="cat sentinel.txt", expected_substring="found", timeout_s=5
    ))
    assert res.ok is True
    assert "found" in res.output
