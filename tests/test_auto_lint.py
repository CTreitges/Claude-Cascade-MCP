"""Tests for `augment_quality_checks_for_python` — the supervisor-side
helper that adds py_compile + ruff checks when the plan touches Python
files and the planner forgot."""

from __future__ import annotations

from cascade.agents.planner import Plan, RepoHint
from cascade.core import augment_quality_checks_for_python
from cascade.workspace import QualityCheck


def _plan(**kw):
    base = dict(
        summary="x",
        steps=[],
        files_to_touch=[],
        acceptance_criteria=[],
        quality_checks=[],
        repo=RepoHint(kind="fresh"),
    )
    base.update(kw)
    return Plan(**base)


def test_no_python_files_no_augmentation():
    plan = _plan(files_to_touch=["README.md", "config.json"])
    out = augment_quality_checks_for_python(plan)
    assert out.quality_checks == []


def test_python_files_add_py_compile():
    plan = _plan(files_to_touch=["src/foo.py", "src/bar.py"])
    out = augment_quality_checks_for_python(plan)
    names = {c.name for c in out.quality_checks}
    assert "py-compile" in names
    cmd = next(c for c in out.quality_checks if c.name == "py-compile").command
    assert "python3 -m py_compile" in cmd
    assert "src/foo.py" in cmd
    assert "src/bar.py" in cmd


def test_existing_py_compile_check_not_duplicated():
    """If the planner already wrote py_compile, don't add a second one."""
    existing = QualityCheck(
        name="custom-compile",
        command="python3 -m py_compile src/x.py",
        timeout_s=10,
    )
    plan = _plan(
        files_to_touch=["src/x.py"],
        quality_checks=[existing],
    )
    out = augment_quality_checks_for_python(plan)
    py_compile_checks = [c for c in out.quality_checks if "py_compile" in c.command]
    assert len(py_compile_checks) == 1


def test_ruff_check_added_when_ruff_available(monkeypatch):
    """Patch shutil.which so the test is deterministic regardless of host."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    plan = _plan(files_to_touch=["app.py"])
    out = augment_quality_checks_for_python(plan)
    names = {c.name for c in out.quality_checks}
    assert "ruff" in names


def test_ruff_check_skipped_when_ruff_missing(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    plan = _plan(files_to_touch=["app.py"])
    out = augment_quality_checks_for_python(plan)
    names = {c.name for c in out.quality_checks}
    assert "ruff" not in names
    # py-compile still added
    assert "py-compile" in names


def test_existing_ruff_check_not_duplicated(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    existing = QualityCheck(
        name="my-ruff",
        command="ruff check src/",
        timeout_s=15,
    )
    plan = _plan(
        files_to_touch=["src/x.py"],
        quality_checks=[existing],
    )
    out = augment_quality_checks_for_python(plan)
    ruff_checks = [c for c in out.quality_checks if "ruff" in c.command]
    assert len(ruff_checks) == 1
