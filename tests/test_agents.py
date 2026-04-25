from __future__ import annotations

import pytest

from cascade.agents.implementer import ImplementerOutput, _coerce
from cascade.agents.planner import Plan
from cascade.agents.reviewer import ReviewResult
from cascade.workspace import FileOp


def test_plan_validates_minimal():
    p = Plan(
        summary="add hello.py",
        steps=["create hello.py"],
        files_to_touch=["hello.py"],
        acceptance_criteria=["hello.py prints hi"],
    )
    assert p.notes is None
    assert p.steps == ["create hello.py"]


def test_review_alias_pass():
    r = ReviewResult.model_validate({"pass": True, "feedback": "", "severity": "low"})
    assert r.passed is True
    assert r.feedback == ""


def test_review_dump_uses_alias():
    r = ReviewResult(passed=False, feedback="missing test", severity="medium")
    dumped = r.model_dump(by_alias=True)
    assert dumped["pass"] is False


def test_implementer_coerce_object():
    raw = '{"ops": [{"op": "write", "path": "a.py", "content": "x"}], "rationale": "ok"}'
    out = _coerce(raw)
    assert isinstance(out, ImplementerOutput)
    assert len(out.ops) == 1
    assert out.ops[0].path == "a.py"
    assert out.rationale == "ok"


def test_implementer_coerce_bare_list():
    raw = '[{"op": "delete", "path": "old.py"}]'
    out = _coerce(raw)
    assert len(out.ops) == 1
    assert out.ops[0].op == "delete"


def test_implementer_coerce_with_fences():
    raw = "```json\n{\"ops\": [{\"op\": \"write\", \"path\": \"f.py\", \"content\": \"y\"}]}\n```"
    out = _coerce(raw)
    assert out.ops[0].path == "f.py"


def test_implementer_coerce_invalid_op_raises():
    raw = '{"ops": [{"op": "rm -rf", "path": "x"}]}'
    with pytest.raises(Exception):
        _coerce(raw)


def test_implementer_output_files_pass_through_to_workspace_validation():
    """The FileOp pydantic check (no abs path) must propagate through ImplementerOutput."""
    raw = '{"ops": [{"op": "write", "path": "/etc/passwd", "content": "x"}]}'
    with pytest.raises(Exception):
        _coerce(raw)


def test_planner_prompt_includes_schema():
    from cascade.agents.planner import _build_prompt

    out = _build_prompt("do something", recall_context=None)
    assert "TASK:" in out
    assert "files_to_touch" in out


def test_planner_prompt_with_recall():
    from cascade.agents.planner import _build_prompt

    out = _build_prompt("do something", recall_context="old finding")
    assert "RELEVANT MEMORIES" in out
    assert "old finding" in out


def test_reviewer_prompt_with_empty_diff():
    from cascade.agents.reviewer import _build_prompt

    plan = Plan(summary="s", steps=[], files_to_touch=[], acceptance_criteria=["a"])
    out = _build_prompt(plan, "")
    assert "empty diff" in out


def test_implementer_user_message_includes_feedback():
    from cascade.agents.implementer import _build_user_message

    plan = Plan(summary="s", steps=["s1"], files_to_touch=["f.py"], acceptance_criteria=["c"])
    msg = _build_user_message(
        plan, workspace_files=["a.py"], feedback="please add tests", iteration=2
    )
    assert "ITERATION: 2" in msg
    assert "REVIEWER FEEDBACK" in msg
    assert "please add tests" in msg
    assert "- a.py" in msg
