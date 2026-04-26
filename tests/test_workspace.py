from __future__ import annotations

import time
from pathlib import Path

import pytest

from cascade.workspace import (
    FileOp,
    Workspace,
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


def test_diff_cumulative_includes_all_committed_iters(ws: Workspace) -> None:
    """The supervisor's integration review needs to see EVERY file the run
    produced, not just the last sub-task's delta. Pre-fix this returned ''.
    """
    ws.apply_ops([FileOp(op="write", path="a.txt", content="A\n")])
    ws.commit_iteration(1)
    ws.apply_ops([FileOp(op="write", path="b.txt", content="B\n")])
    ws.commit_iteration(2)
    ws.apply_ops([FileOp(op="write", path="c.txt", content="C\n")])  # uncommitted
    cum = ws.diff_cumulative()
    # All three files must appear in the cumulative diff.
    assert "a.txt" in cum
    assert "b.txt" in cum
    assert "c.txt" in cum
    # Per-iter diff should still only show the latest staged change.
    assert "c.txt" in ws.diff()
    assert "a.txt" not in ws.diff()


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


def test_create_is_idempotent_when_dir_exists(tmp_path: Path) -> None:
    """Pre-existing workspace dir must not crash create() — fixes a
    FileExistsError seen on resumed/retried spawns."""
    base = tmp_path / "ws-base"
    base.mkdir()
    (base / "preexisting-id").mkdir()
    (base / "preexisting-id" / "marker.txt").write_text("kept")
    ws = Workspace.create(base, task_id="preexisting-id")
    assert ws.root == (base / "preexisting-id").resolve()
    # User-pre-existing file untouched, git was bootstrapped:
    assert (base / "preexisting-id" / "marker.txt").read_text() == "kept"
    assert (base / "preexisting-id" / ".git").exists()
    # Second call on same id should not crash either
    ws2 = Workspace.create(base, task_id="preexisting-id")
    assert ws2.root == ws.root


def test_attach_preserves_user_files_and_bootstraps_git(tmp_path: Path) -> None:
    # Pre-existing dir without git: attach now bootstraps a local .git
    # so diffs work, but user files stay byte-identical.
    repo = tmp_path / "existing"
    repo.mkdir()
    (repo / "keep.txt").write_text("keep")
    ws = Workspace.attach(repo)
    assert ws.root == repo.resolve()
    assert (repo / ".git").exists()  # bootstrap so changed_paths() works
    assert (repo / "keep.txt").read_text() == "keep"  # user file untouched
    assert ws.is_attached is True
    assert ws.base_ref is not None
    # changed_paths is empty right after attach; only Cascade-written files appear later
    assert ws.changed_paths() == []
