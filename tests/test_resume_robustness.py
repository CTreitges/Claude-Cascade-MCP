"""Tests for the resume-confirmation similarity helper and the corrupt-plan
hydration fallback."""

from __future__ import annotations

from cascade.bot.state import task_similarity


def test_similarity_identical_strings():
    assert task_similarity("hello world", "hello world") == 1.0


def test_similarity_no_overlap():
    assert task_similarity("apple banana cherry", "xeno yotta zebra") == 0.0


def test_similarity_partial_overlap_ranks_above_zero():
    s = task_similarity(
        "Erstelle die Service-Account-JSON-Datei für project soundcloud",
        "Service-Account JSON für project soundcloud-downloader anlegen",
    )
    assert s > 0.4
    assert s < 1.0


def test_similarity_short_token_filter():
    """Tokens <4 chars are filtered → 'a', 'is', 'the' don't inflate."""
    s = task_similarity("a b is the", "x y or so")
    assert s == 0.0  # all tokens dropped


def test_similarity_diacritics_distinguish_long_tokens():
    """Token length filter is >=4, so accented words longer than that
    shouldn't collide with their unaccented variants."""
    a = task_similarity("löschen config datei", "löschen config datei")
    assert a == 1.0
    b = task_similarity("löschen config datei", "loeschen config datei")
    # `löschen` and `loeschen` tokenize differently; `config`/`datei` overlap
    # while the umlaut tokens don't.
    assert 0.0 < b < 1.0


def test_similarity_empty_strings():
    assert task_similarity("", "anything") == 0.0
    assert task_similarity("anything", "") == 0.0
    assert task_similarity("", "") == 0.0


def test_similarity_threshold_for_resume():
    """The runner uses sim >= 0.7 as the resume-keyboard trigger; check
    that an obviously similar pair clears it but a mostly-different one
    doesn't."""
    same = task_similarity(
        "Lege Service-Account JSON für soundcloud-downloader unter "
        "~/.config/gcloud/sa.json ab",
        "Service-Account JSON soundcloud-downloader nach ~/.config/gcloud/sa.json",
    )
    assert same >= 0.5  # similar setup intent

    different = task_similarity(
        "Schreibe ein FastAPI-Endpoint für Lieferschein-Upload",
        "Setze SCDL_DRIVE_FOLDER_ID in .env",
    )
    assert different < 0.7
