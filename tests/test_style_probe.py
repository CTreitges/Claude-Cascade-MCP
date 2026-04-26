from __future__ import annotations

from pathlib import Path

from cascade.style_probe import format_style_hints, probe_repo_style


def test_probe_empty_dir_returns_empty(tmp_path: Path):
    assert probe_repo_style(tmp_path) == {}


def test_probe_pyproject_picks_up_ruff_line_length(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 120\n[tool.ruff.lint]\nselect = ["E", "F", "I"]\n',
    )
    h = probe_repo_style(tmp_path)
    assert h["line_length"] == "120"
    assert "ruff_select" in h
    assert "E" in h["ruff_select"]


def test_probe_picks_up_pytest_addopts(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-q --tb=short"\n',
    )
    h = probe_repo_style(tmp_path)
    assert h["test_runner"] == "pytest"
    assert "--tb=short" in h["pytest_addopts"]


def test_probe_picks_up_mypy(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    h = probe_repo_style(tmp_path)
    assert h["typecheck"] == "mypy"


def test_probe_lockfiles_dictate_package_manager(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("# fake")
    h = probe_repo_style(tmp_path)
    assert h["package_manager"] == "uv"


def test_probe_editorconfig_line_length_fallback(tmp_path: Path):
    (tmp_path / ".editorconfig").write_text(
        "[*.py]\nindent_size = 4\nmax_line_length = 100\n",
    )
    h = probe_repo_style(tmp_path)
    assert h["indent_size"] == "4"
    assert h["line_length"] == "100"


def test_format_hints_returns_none_for_empty():
    assert format_style_hints({}) is None


def test_format_hints_de_block_has_german_header():
    block = format_style_hints({"line_length": "120"}, lang="de")
    assert block is not None
    assert "REPO-STIL" in block
    assert "120" in block


def test_format_hints_en_block_has_english_header():
    block = format_style_hints({"line_length": "120"}, lang="en")
    assert block is not None
    assert "REPO STYLE" in block
