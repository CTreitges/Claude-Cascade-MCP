from __future__ import annotations

from pathlib import Path

from cascade.workspace import FileOp, Workspace


def test_read_files_returns_existing_only(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r1")
    ws.apply_ops([FileOp(op="write", path="a.py", content="print('a')\n")])
    out = ws.read_files(["a.py", "missing.py"])
    assert "a.py" in out and "missing.py" not in out


def test_read_files_truncates_large_file(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r2")
    big = "x" * 5000
    ws.apply_ops([FileOp(op="write", path="big.py", content=big)])
    out = ws.read_files(["big.py"], max_per_file=1000)
    assert "truncated" in out["big.py"]
    assert out["big.py"].startswith("x" * 1000)


def test_read_files_global_budget(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r3")
    ws.apply_ops(
        [
            FileOp(op="write", path="a.txt", content="A" * 800),
            FileOp(op="write", path="b.txt", content="B" * 800),
            FileOp(op="write", path="c.txt", content="C" * 800),
        ]
    )
    out = ws.read_files(["a.txt", "b.txt", "c.txt"], max_bytes=1500, max_per_file=900)
    # only a.txt and partial b.txt fit
    total = sum(len(v) for v in out.values())
    assert total <= 1600  # tiny overshoot allowed for the truncation marker


def test_read_files_path_escape_blocked(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r4")
    out = ws.read_files(["../escape.txt", "/etc/passwd"])
    assert out == {}


def test_candidate_context_files_picks_exact_match(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r5")
    ws.apply_ops(
        [
            FileOp(op="write", path="src/foo.py", content="x"),
            FileOp(op="write", path="README.md", content="y"),
        ]
    )
    ws.commit_iteration(1)
    picks = ws.candidate_context_files(["src/foo.py"])
    assert picks == ["src/foo.py"]


def test_candidate_context_files_basename_fallback(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r6")
    ws.apply_ops(
        [
            FileOp(op="write", path="src/foo.py", content="x"),
            FileOp(op="write", path="README.md", content="y"),
        ]
    )
    ws.commit_iteration(1)
    # plan referenced just "foo.py" without folder
    picks = ws.candidate_context_files(["foo.py"])
    assert "src/foo.py" in picks


def test_candidate_context_files_respects_limit(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "wb", task_id="r7")
    ws.apply_ops(
        [FileOp(op="write", path=f"f{i}.py", content="x") for i in range(20)]
    )
    ws.commit_iteration(1)
    hints = [f"f{i}.py" for i in range(20)]
    picks = ws.candidate_context_files(hints, limit=5)
    assert len(picks) == 5
