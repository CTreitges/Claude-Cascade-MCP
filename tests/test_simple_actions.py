from __future__ import annotations

from pathlib import Path

import pytest

from cascade.simple_actions import is_known_kind, run_action


async def test_is_known_kind_covers_all_handlers():
    for k in ("write_file", "place_file", "edit_env", "read_file"):
        assert is_known_kind(k)
    assert not is_known_kind("rm_rf")
    assert not is_known_kind("")


async def test_write_file_under_tmp(tmp_path: Path, monkeypatch):
    # Force /tmp into the allowed roots check by writing under tmp_path,
    # which on most systems IS under /tmp.
    target = tmp_path / "out.txt"
    res = await run_action({
        "kind": "write_file",
        "params": {"target": str(target), "content": "hello\n"},
    })
    assert res.ok, res.error
    assert target.read_text() == "hello\n"
    assert str(target) in res.files_touched


async def test_write_file_rejects_outside_allowlist(tmp_path: Path):
    # /etc/ is never in the allowlist
    res = await run_action({
        "kind": "write_file",
        "params": {"target": "/etc/cascade-test.txt", "content": "x"},
    })
    assert not res.ok
    assert "outside allowed roots" in (res.error or "")


async def test_edit_env_appends_then_updates(tmp_path: Path):
    env_file = tmp_path / ".env"
    res1 = await run_action({
        "kind": "edit_env",
        "params": {"target": str(env_file), "key": "FOO", "value": "1"},
    })
    assert res1.ok
    assert env_file.read_text() == "FOO=1\n"

    res2 = await run_action({
        "kind": "edit_env",
        "params": {"target": str(env_file), "key": "BAR", "value": "2"},
    })
    assert res2.ok
    assert "FOO=1" in env_file.read_text()
    assert "BAR=2" in env_file.read_text()

    res3 = await run_action({
        "kind": "edit_env",
        "params": {"target": str(env_file), "key": "FOO", "value": "99"},
    })
    assert res3.ok
    text = env_file.read_text()
    assert "FOO=99" in text
    assert "FOO=1" not in text  # replaced, not duplicated


async def test_edit_env_rejects_invalid_key(tmp_path: Path):
    env_file = tmp_path / ".env"
    res = await run_action({
        "kind": "edit_env",
        "params": {"target": str(env_file), "key": "lower-case", "value": "x"},
    })
    assert not res.ok
    assert "invalid env key" in (res.error or "")


async def test_place_file_copies_with_mode(tmp_path: Path):
    src = tmp_path / "source.json"
    src.write_text('{"k": "v"}')
    dst = tmp_path / "dst" / "creds.json"
    res = await run_action({
        "kind": "place_file",
        "params": {"source": str(src), "target": str(dst), "mode": 0o600},
    })
    assert res.ok, res.error
    assert dst.read_text() == '{"k": "v"}'
    assert (dst.stat().st_mode & 0o777) == 0o600


async def test_read_file(tmp_path: Path):
    f = tmp_path / "log.txt"
    f.write_text("line one\nline two\n")
    res = await run_action({"kind": "read_file", "params": {"target": str(f)}})
    assert res.ok
    assert "line one" in res.output


async def test_unknown_kind_returns_failure():
    res = await run_action({"kind": "rm_rf", "params": {}})
    assert not res.ok
    assert "unknown kind" in (res.error or "")


@pytest.mark.parametrize("missing", [{}, {"kind": ""}, {"kind": None}])
async def test_malformed_action_does_not_crash(missing):
    res = await run_action(missing)
    assert not res.ok
