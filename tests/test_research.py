from __future__ import annotations

from cascade.research import detect_libraries, needs_web_search


def test_detect_libraries_keywords_de():
    text = "Erstelle ein FastAPI-Service mit Pydantic-Modellen und SQLAlchemy."
    libs = detect_libraries(text)
    # ordered by curated list, dedup, lowercase-match
    assert "pydantic" in libs
    assert "fastapi" in libs
    assert "sqlalchemy" in libs


def test_detect_libraries_import_pattern():
    text = "import openpyxl\nfrom matplotlib import pyplot"
    libs = detect_libraries(text, max_hits=4)
    # the keyword list catches openpyxl + matplotlib, no need for the regex
    assert "openpyxl" in libs
    assert "matplotlib" in libs


def test_detect_libraries_max_hits_bounded():
    text = "pydantic fastapi flask django sqlalchemy click pytest httpx"
    libs = detect_libraries(text, max_hits=3)
    assert len(libs) == 3


def test_detect_libraries_empty_input_safe():
    assert detect_libraries("") == []
    assert detect_libraries("hello world") == []


def test_needs_web_search_de_triggers():
    assert needs_web_search("Was ist der aktuelle Preis von Bitcoin?", "de")
    assert needs_web_search("Zeig mir die neueste Version von Python", "de")
    assert not needs_web_search("schreib eine simple hello.py", "de")


def test_needs_web_search_en_triggers():
    assert needs_web_search("what's the latest pricing for Anthropic API?", "en")
    assert needs_web_search("compare GPT-5 vs Sonnet 4.6 today", "en")
    assert not needs_web_search("write a simple hello.py", "en")


async def test_gather_external_context_no_signals_returns_none():
    from cascade.research import gather_external_context
    out = await gather_external_context("schreibe foo.py mit print(hi)", lang="de")
    # No library + no web-trigger → None (avoids unnecessary HTTP calls)
    assert out is None


async def test_gather_external_context_disabled_returns_none():
    from cascade.research import gather_external_context
    out = await gather_external_context(
        "use pydantic and fastapi",
        enabled_context7=False,
        enabled_websearch=False,
    )
    assert out is None
