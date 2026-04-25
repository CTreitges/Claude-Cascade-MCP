"""Tests for the new bot helpers (markdown escape, long-output split)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import bot


def test_md_escape_handles_special_chars():
    assert bot._md_escape("foo_bar") == "foo\\_bar"
    assert bot._md_escape("*x*") == "\\*x\\*"
    assert bot._md_escape("`quoted`") == "\\`quoted\\`"
    assert bot._md_escape("a[b]c") == "a\\[b]c"
    assert bot._md_escape("plain") == "plain"
    assert bot._md_escape("") == ""
    assert bot._md_escape(None) == ""


async def test_send_long_short_string_one_call():
    msg = AsyncMock()
    await bot._send_long(msg, "hello", chunk=3500)
    assert msg.reply_text.await_count == 1


async def test_send_long_splits_when_over_chunk():
    msg = AsyncMock()
    text = "x" * 7100  # 3 chunks at 3500
    await bot._send_long(msg, text, chunk=3500)
    assert msg.reply_text.await_count == 3
    # First message should carry "(1/3)"
    first = msg.reply_text.await_args_list[0]
    assert "(1/3)" in first.args[0]


async def test_send_long_empty_is_noop():
    msg = AsyncMock()
    await bot._send_long(msg, "")
    assert msg.reply_text.await_count == 0


async def test_send_long_code_wraps_in_block():
    msg = AsyncMock()
    await bot._send_long(msg, "x" * 50, code=True, chunk=3500)
    sent = msg.reply_text.await_args_list[0].args[0]
    assert sent.startswith("```\n")
    assert sent.endswith("\n```")


async def test_send_short_text_no_progress_marker():
    msg = AsyncMock()
    await bot._send_long(msg, "abc", chunk=3500)
    sent = msg.reply_text.await_args_list[0].args[0]
    assert sent == "abc"  # no "(1/1)" prefix when only one piece
