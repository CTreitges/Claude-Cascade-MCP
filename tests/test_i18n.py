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
    assert "Schicke" in out and "/help" in out


def test_help_string_english():
    out = t("help", lang="en")
    assert "Send" in out and "/help" in out


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
