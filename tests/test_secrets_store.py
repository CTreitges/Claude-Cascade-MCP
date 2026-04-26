from __future__ import annotations

from pathlib import Path

import pytest

from cascade.secrets_store import load_secrets, set_secret, unset_secret


def test_set_secret_creates_file_and_chmods(tmp_path: Path):
    p = tmp_path / "secrets.env"
    out = set_secret("OPENAI_API_KEY", "sk-test-123", path=p)
    assert out == p
    assert p.is_file()
    assert p.read_text() == "OPENAI_API_KEY=sk-test-123\n"
    # 0o600 — readable only by owner. Skip on filesystems without u-only.
    mode = p.stat().st_mode & 0o777
    if mode:  # some filesystems return 0 for chmod
        assert mode & 0o077 == 0


def test_set_secret_updates_existing_key(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text("OPENAI_API_KEY=old\nGLM_API_KEY=glm-x\n")
    set_secret("OPENAI_API_KEY", "new", path=p)
    text = p.read_text()
    assert "OPENAI_API_KEY=new" in text
    assert "OPENAI_API_KEY=old" not in text
    assert "GLM_API_KEY=glm-x" in text  # untouched


def test_set_secret_preserves_comments_and_blank_lines(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text("# my secrets\n\nFOO=1\n# trailing comment\n")
    set_secret("BAR", "2", path=p)
    text = p.read_text()
    assert "# my secrets" in text
    assert "# trailing comment" in text
    assert "BAR=2" in text


def test_set_secret_rejects_invalid_key(tmp_path: Path):
    p = tmp_path / "secrets.env"
    with pytest.raises(ValueError):
        set_secret("lower-case", "x", path=p)
    with pytest.raises(ValueError):
        set_secret("123_BAD_START", "x", path=p)


def test_load_secrets_skips_blanks_and_comments(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text(
        "# header\n\nA=1\nB=2\n# inline comment line\n  C=3  \n",
    )
    out = load_secrets(path=p)
    assert out == {"A": "1", "B": "2", "C": "3"}


def test_load_secrets_returns_empty_when_missing(tmp_path: Path):
    assert load_secrets(path=tmp_path / "nope.env") == {}


def test_load_secrets_later_value_wins(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text("FOO=first\nFOO=second\n")
    out = load_secrets(path=p)
    assert out["FOO"] == "second"


def test_unset_secret_removes_key(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text("FOO=1\nBAR=2\n")
    assert unset_secret("FOO", path=p) is True
    assert "FOO=" not in p.read_text()
    assert "BAR=2" in p.read_text()
    assert unset_secret("FOO", path=p) is False  # already gone


def test_set_secret_does_not_collapse_duplicates_into_extra_blank(tmp_path: Path):
    p = tmp_path / "secrets.env"
    p.write_text("FOO=1\nFOO=duplicate\nBAR=2\n")
    set_secret("FOO", "final", path=p)
    text = p.read_text()
    # Both old FOO lines must be gone; only one FOO= remains
    assert text.count("FOO=") == 1
    assert "FOO=final" in text
    assert "BAR=2" in text
