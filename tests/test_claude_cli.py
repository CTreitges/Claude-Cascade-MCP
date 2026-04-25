from __future__ import annotations

import pytest

from cascade.claude_cli import ClaudeCliError, parse_json_payload


def test_parse_plain_json():
    assert parse_json_payload('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    text = "```json\n{\"a\": 1, \"b\": [2,3]}\n```"
    assert parse_json_payload(text) == {"a": 1, "b": [2, 3]}


def test_parse_fenced_no_lang():
    text = "```\n{\"x\": true}\n```"
    assert parse_json_payload(text) == {"x": True}


def test_parse_json_with_leading_prose():
    text = "Sure, here you go:\n{\"k\": \"v\"}\nHope it helps."
    assert parse_json_payload(text) == {"k": "v"}


def test_parse_json_with_nested_braces():
    text = 'Result: {"a": {"b": {"c": 1}}, "d": [1,2,3]}'
    assert parse_json_payload(text) == {"a": {"b": {"c": 1}}, "d": [1, 2, 3]}


def test_parse_raises_when_no_json():
    with pytest.raises(ClaudeCliError):
        parse_json_payload("nothing useful here")


def test_parse_raises_on_unbalanced():
    with pytest.raises(ClaudeCliError):
        parse_json_payload('{"a": 1')
