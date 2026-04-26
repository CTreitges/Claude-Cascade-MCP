"""Tests for `_diff_quality_signals` — diff-size + missing-test heuristics
that the reviewer's prompt builder uses."""

from __future__ import annotations

from cascade.agents.reviewer import _diff_quality_signals


def test_empty_diff_returns_no_signals():
    assert _diff_quality_signals("") == []


def test_small_diff_no_size_warning():
    diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n+def ok():\n+    return 1\n"
    sigs = _diff_quality_signals(diff)
    assert all("large" not in s for s in sigs)


def test_large_diff_triggers_size_warning():
    plus = "+" + "a" * 80
    diff = "diff --git a/big.py b/big.py\n+++ b/big.py\n" + "\n".join(
        [plus] * 600
    )
    sigs = _diff_quality_signals(diff)
    assert any("large" in s and "decompose" in s.lower() for s in sigs)


def test_new_function_without_test_flags_coverage():
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "+def newfunc():\n"
        "+    return 42\n"
    )
    sigs = _diff_quality_signals(diff)
    assert any("test" in s.lower() and "coverage" in s.lower() for s in sigs)


def test_new_function_with_test_change_no_coverage_flag():
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "+def newfunc():\n"
        "+    return 42\n"
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
        "+def test_newfunc():\n"
        "+    assert newfunc() == 42\n"
    )
    sigs = _diff_quality_signals(diff)
    assert all("coverage" not in s.lower() for s in sigs)


def test_changes_only_in_test_files_no_coverage_flag():
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "+def test_more():\n"
        "+    assert True\n"
    )
    sigs = _diff_quality_signals(diff)
    assert all("coverage" not in s.lower() for s in sigs)
