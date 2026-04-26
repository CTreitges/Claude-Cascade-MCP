from cascade.i18n import de_en


def test_de_en_returns_german_by_default():
    assert de_en("Hallo", "Hello") == "Hallo"


def test_de_en_returns_english_when_lang_en():
    assert de_en("Hallo", "Hello", "en") == "Hello"


def test_de_en_returns_german_for_explicit_de():
    assert de_en("Hallo", "Hello", "de") == "Hallo"


def test_de_en_falls_back_to_german_for_unknown_lang():
    assert de_en("Hallo", "Hello", "fr") == "Hallo"
    assert de_en("Hallo", "Hello", "") == "Hallo"
    assert de_en("Hallo", "Hello", None) == "Hallo"  # type: ignore[arg-type]


def test_de_en_handles_empty_strings():
    assert de_en("", "Hello") == ""
    assert de_en("", "Hello", "en") == "Hello"


def test_de_en_with_multiline_strings():
    de = "Zeile 1\nZeile 2"
    en = "Line 1\nLine 2"
    assert de_en(de, en) == de
    assert de_en(de, en, "en") == en
