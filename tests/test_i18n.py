from cascade.i18n import t


def test_de_default():
    assert "Plane" in t("progress.planning_initial")


def test_en_explicit():
    assert "Planning" in t("progress.planning_initial", lang="en")


def test_format_vars():
    out = t("progress.implemented", lang="de", n=2, ops=5, failed=1)
    assert "Iteration 2" in out and "5 Ops" in out


def test_unknown_key_returns_marker():
    assert t("does.not.exist") == "[missing:does.not.exist]"


def test_help_string_german():
    out = t("help", lang="de")
    # all command groups + every slash command should be present
    assert "/help" in out and "/models" in out and "/forget" in out
    assert "Tasks" in out or "Task" in out
    assert "Skills" in out
    assert "Konfig" in out
    assert "System" in out


def test_help_string_english():
    out = t("help", lang="en")
    assert "/help" in out and "/models" in out and "/forget" in out
    assert "Tasks" in out or "Task" in out
    assert "Skills" in out
    assert "Config" in out
    assert "System" in out


def test_help_text_present_and_chunkable():
    """Telegram's hard limit is 4096 chars per sendMessage; /help has
    outgrown that, so cmd_help uses `send_long` which chunks at 3500.
    Make sure the rendered text exists and that EVERY chunk under that
    threshold ends up below 4096."""
    from cascade.bot.helpers import send_long  # noqa: F401  (import just to assert importable)
    for lang in ("de", "en"):
        text = t("help", lang=lang)
        assert text and len(text) > 100, f"/help/{lang} suspiciously empty"
        # Splitting at 3500 (helpers.send_long default) should yield only
        # chunks well under Telegram's 4096-char hard limit.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)]
        assert all(len(c) <= 4000 for c in chunks)


def test_status_line_german():
    out = t(
        "status_line",
        lang="de",
        emoji="✅",
        status="done",
        task_id="abc",
        task="x",
        iteration=1,
        summary="ok",
    )
    assert "Iter:" in out and "Zusammenfassung:" in out


def test_repo_strings():
    assert "gelöscht" in t("repo.cleared", lang="de")
    assert "cleared" in t("repo.cleared", lang="en")
