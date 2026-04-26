"""Tests for the pre-write content validation in workspace.apply_ops.

The implementer is the one writing files; bad output here used to slip
through to the quality_checks layer where the error message ("pytest
exited 1") was unhelpful. With pre-validation the implementer gets a
precise reason at the file-write site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cascade.workspace import FileOp, Workspace


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "ws", task_id="vt")


def test_write_python_with_syntax_error_is_rejected(ws: Workspace):
    bad = "def foo(:\n    pass\n"
    res = ws.apply_ops([FileOp(op="write", path="x.py", content=bad)])
    assert len(res) == 1
    assert res[0].ok is False
    assert "syntax" in res[0].detail.lower()
    # File must NOT have been written
    assert not (ws.root / "x.py").exists()


def test_write_valid_python_passes(ws: Workspace):
    good = "def foo():\n    return 1\n"
    res = ws.apply_ops([FileOp(op="write", path="ok.py", content=good)])
    assert res[0].ok, res[0].detail
    assert (ws.root / "ok.py").read_text() == good


def test_write_python_with_only_pass_is_rejected(ws: Workspace):
    stub = "def foo():\n    pass\n"
    res = ws.apply_ops([FileOp(op="write", path="stub.py", content=stub)])
    assert res[0].ok is False
    assert "stub" in res[0].detail.lower()


def test_write_python_with_only_ellipsis_is_rejected(ws: Workspace):
    stub = "def foo():\n    ...\n"
    res = ws.apply_ops([FileOp(op="write", path="ell.py", content=stub)])
    assert res[0].ok is False
    assert "stub" in res[0].detail.lower()


def test_write_python_with_raise_notimplemented_is_rejected(ws: Workspace):
    stub = (
        "def real():\n"
        "    return 1\n"
        "def todo():\n"
        "    raise NotImplementedError('later')\n"
    )
    res = ws.apply_ops([FileOp(op="write", path="r.py", content=stub)])
    assert res[0].ok is False
    assert "todo" in res[0].detail.lower()


def test_write_python_with_docstring_only_treated_as_stub(ws: Workspace):
    """A function with just a docstring + nothing else is still not real."""
    stub = (
        'def foo():\n'
        '    """does the thing."""\n'
        '    pass\n'
    )
    res = ws.apply_ops([FileOp(op="write", path="ds.py", content=stub)])
    assert res[0].ok is False
    assert "stub" in res[0].detail.lower()


def test_write_python_with_real_body_passes_even_with_docstring(ws: Workspace):
    good = (
        'def foo():\n'
        '    """does the thing."""\n'
        '    return 42\n'
    )
    res = ws.apply_ops([FileOp(op="write", path="g.py", content=good)])
    assert res[0].ok, res[0].detail


def test_bare_raise_inside_except_is_not_a_stub(ws: Workspace):
    """`raise` (no args) inside a try/except is a re-raise — NOT a stub."""
    fine = (
        "def foo():\n"
        "    try:\n"
        "        do()\n"
        "    except Exception:\n"
        "        raise\n"
        "\n"
        "def do():\n"
        "    return 1\n"
    )
    res = ws.apply_ops([FileOp(op="write", path="reraise.py", content=fine)])
    assert res[0].ok, res[0].detail


def test_write_invalid_json_is_rejected(ws: Workspace):
    bad = '{"a": 1,'
    res = ws.apply_ops([FileOp(op="write", path="conf.json", content=bad)])
    assert res[0].ok is False
    assert "json" in res[0].detail.lower()


def test_write_valid_json_passes(ws: Workspace):
    good = '{"a": 1, "b": [2, 3]}'
    res = ws.apply_ops([FileOp(op="write", path="ok.json", content=good)])
    assert res[0].ok, res[0].detail


def test_write_invalid_toml_is_rejected(ws: Workspace):
    bad = 'a = "missing close quote\n'
    res = ws.apply_ops([FileOp(op="write", path="conf.toml", content=bad)])
    assert res[0].ok is False
    assert "toml" in res[0].detail.lower()


def test_validation_does_not_apply_to_unrecognised_extensions(ws: Workspace):
    """Plain text / markdown / shell scripts must NOT be syntax-checked."""
    res = ws.apply_ops([
        FileOp(op="write", path="README.md", content="# Hello\n\nworld"),
        FileOp(op="write", path="run.sh", content="#!/bin/bash\necho hi\n"),
        FileOp(op="write", path="notes.txt", content="random text)" + "\n"),
    ])
    assert all(r.ok for r in res), [r.detail for r in res]


def test_edit_overwrite_validates_too(ws: Workspace):
    """When edit falls back to full-content overwrite, the new content
    must pass validation just like a fresh write."""
    # First seed a valid file
    ws.apply_ops([FileOp(op="write", path="a.py", content="def x(): return 1\n")])
    # Now try an edit-overwrite with broken content
    res = ws.apply_ops([FileOp(
        op="edit", path="a.py", content="def x(:\n    return 1\n",
    )])
    assert res[0].ok is False
    assert "syntax" in res[0].detail.lower()
    # Original content unchanged
    assert (ws.root / "a.py").read_text() == "def x(): return 1\n"


def test_edit_find_replace_validates_result(ws: Workspace):
    """find+replace must produce a syntactically valid file too."""
    ws.apply_ops([FileOp(op="write", path="b.py", content="def y(): return 2\n")])
    res = ws.apply_ops([FileOp(
        op="edit", path="b.py",
        find="return 2", replace="return 2  # but wait( missing close",
    )])
    # The new content is `def y(): return 2  # but wait( missing close\n`
    # — this is actually valid Python (the # makes it a comment). So we
    # confirm the validator only complains about REAL breakage.
    assert res[0].ok, res[0].detail

    # Now break it for real
    res2 = ws.apply_ops([FileOp(
        op="edit", path="b.py",
        find="def y(): return 2  # but wait( missing close",
        replace="def y(:\n    return 2",
    )])
    assert res2[0].ok is False


def test_function_inside_method_is_also_checked(ws: Workspace):
    """Stubs nested inside a class method should be caught."""
    code = (
        "class A:\n"
        "    def meth(self):\n"
        "        return 1\n"
        "    def stub(self):\n"
        "        raise NotImplementedError\n"
    )
    res = ws.apply_ops([FileOp(op="write", path="cls.py", content=code)])
    assert res[0].ok is False
    assert "stub" in res[0].detail.lower()
