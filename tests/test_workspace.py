from __future__ import annotations

import time
from pathlib import Path

import pytest

from cascade.workspace import (
    FileOp,
    Workspace,
    WorkspaceError,
    cleanup_old_workspaces,
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "ws-base", task_id="t-001")


def test_create_inits_git_repo(ws: Workspace) -> None:
    assert (ws.root / ".git").is_dir()
    assert ws.root.name == "t-001"


def test_write_creates_file_with_content(ws: Workspace) -> None:
    res = ws.apply_ops([FileOp(op="write", path="hello.py", content="print('hi')\n")])
    assert all(r.ok for r in res)
    assert (ws.root / "hello.py").read_text() == "print('hi')\n"


def test_write_creates_nested_dirs(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="a/b/c.txt", content="x")])
    assert (ws.root / "a/b/c.txt").read_text() == "x"


def test_edit_with_find_replace_unique(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="f.txt", content="alpha beta gamma")])
    res = ws.apply_ops([FileOp(op="edit", path="f.txt", find="beta", replace="BETA")])
    assert res[0].ok, res[0].detail
    assert (ws.root / "f.txt").read_text() == "alpha BETA gamma"


def test_edit_fails_when_find_missing(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="f.txt", content="alpha")])
    res = ws.apply_ops([FileOp(op="edit", path="f.txt", find="zzz", replace="x")])
    assert not res[0].ok
    assert "not found" in res[0].detail


def test_edit_fails_when_find_not_unique(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="f.txt", content="x x x")])
    res = ws.apply_ops([FileOp(op="edit", path="f.txt", find="x", replace="y")])
    assert not res[0].ok
    assert "not unique" in res[0].detail


def test_delete_removes_file(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="g.txt", content="bye")])
    res = ws.apply_ops([FileOp(op="delete", path="g.txt")])
    assert res[0].ok
    assert not (ws.root / "g.txt").exists()


def test_delete_missing_fails(ws: Workspace) -> None:
    res = ws.apply_ops([FileOp(op="delete", path="nope.txt")])
    assert not res[0].ok


def test_path_escape_blocked_via_dotdot(ws: Workspace) -> None:
    res = ws.apply_ops([FileOp(op="write", path="../escape.txt", content="x")])
    assert not res[0].ok
    assert "escape" in res[0].detail.lower()
    # And nothing was written outside
    assert not (ws.root.parent / "escape.txt").exists()


def test_path_escape_blocked_via_symlink_target(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "ws-base", task_id="link")
    outside = tmp_path / "outside.txt"
    outside.write_text("orig")
    # Create a symlink inside the workspace pointing outside, then try to write through it.
    (ws.root / "link.txt").symlink_to(outside)
    # Writing via the symlink is allowed (Path.resolve resolves symlinks),
    # but our sandbox check should reject it because resolved path escapes root.
    res = ws.apply_ops([FileOp(op="write", path="link.txt", content="hijack")])
    assert not res[0].ok
    assert outside.read_text() == "orig"


def test_absolute_path_rejected_by_validator() -> None:
    with pytest.raises(Exception):
        FileOp(op="write", path="/etc/passwd", content="x")


def test_diff_includes_new_file(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="hello.py", content="print('hi')\n")])
    diff = ws.diff()
    assert "hello.py" in diff
    assert "+print('hi')" in diff


def test_commit_iteration_starts_clean_for_next_diff(ws: Workspace) -> None:
    ws.apply_ops([FileOp(op="write", path="a.txt", content="A")])
    ws.commit_iteration(1)
    diff = ws.diff()
    assert diff == ""  # nothing new since commit
    ws.apply_ops([FileOp(op="write", path="b.txt", content="B")])
    diff2 = ws.diff()
    assert "b.txt" in diff2 and "a.txt" not in diff2


def test_list_files(ws: Workspace) -> None:
    ws.apply_ops(
        [
            FileOp(op="write", path="x.py", content="x"),
            FileOp(op="write", path="d/y.py", content="y"),
        ]
    )
    ws.commit_iteration(1)
    files = set(ws.list_files())
    assert files == {"x.py", "d/y.py"}


def test_dict_ops_are_validated(ws: Workspace) -> None:
    res = ws.apply_ops([{"op": "write", "path": "k.txt", "content": "k"}])
    assert res[0].ok
    assert (ws.root / "k.txt").read_text() == "k"


def test_cleanup_old_workspaces(tmp_path: Path) -> None:
    base = tmp_path / "ws"
    base.mkdir()
    old = base / "old"
    old.mkdir()
    fresh = base / "fresh"
    fresh.mkdir()
    # Backdate `old` by 30 days
    old_time = time.time() - 30 * 86400
    import os

    os.utime(old, (old_time, old_time))

    import asyncio

    removed = asyncio.run(cleanup_old_workspaces(base, retention_days=7))
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_handles_missing_dir(tmp_path: Path) -> None:
    import asyncio

    removed = asyncio.run(cleanup_old_workspaces(tmp_path / "nope", retention_days=7))
    assert removed == 0


def test_attach_does_not_modify_existing_repo(tmp_path: Path) -> None:
    # Pre-existing dir without git
    repo = tmp_path / "existing"
    repo.mkdir()
    (repo / "keep.txt").write_text("keep")
    ws = Workspace.attach(repo)
    assert ws.root == repo.resolve()
    assert not (repo / ".git").exists()  # attach didn't init
    assert (repo / "keep.txt").read_text() == "keep"
