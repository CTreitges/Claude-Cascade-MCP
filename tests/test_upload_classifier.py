"""Tests for cascade.upload_classifier.

These were previously embedded inside test_e2e_drive_setup; pulling them
into a focused unit-test file keeps the e2e test about the e2e flow and
gives the classifier its own home now that it lives in its own module.
"""

from __future__ import annotations

import json

from cascade.upload_classifier import (
    classify_uploaded_json,
    classify_uploaded_text,
)


# ---- JSON classifier ------------------------------------------------------


def test_classify_unparseable_returns_none():
    assert classify_uploaded_json("not-json") is None
    assert classify_uploaded_json("") is None


def test_classify_json_array_falls_back_to_generic_json():
    out = classify_uploaded_json("[1, 2, 3]")
    assert out["kind"] == "generic_json"
    assert out["auto_stage_safe"] is False


def test_classify_google_service_account_is_auto_stage_safe():
    body = json.dumps({
        "type": "service_account",
        "project_id": "my-proj",
        "client_email": "sa@my-proj.iam.gserviceaccount.com",
        "private_key": "...",
    })
    out = classify_uploaded_json(body)
    assert out["kind"] == "google_service_account"
    assert out["project_id"] == "my-proj"
    assert "my-proj" in out["suggested_target"]
    assert out["auto_stage_safe"] is True


def test_classify_oauth_installed_client():
    body = json.dumps({
        "installed": {
            "client_id": "abc.apps.googleusercontent.com",
            "client_secret": "secret",
        },
    })
    out = classify_uploaded_json(body)
    assert out["kind"] == "google_oauth_client"
    assert out["auto_stage_safe"] is True


def test_classify_oauth_web_client():
    body = json.dumps({
        "web": {
            "client_id": "abc",
            "client_secret": "x",
        },
    })
    out = classify_uploaded_json(body)
    assert out["kind"] == "google_oauth_client"


def test_classify_aws_credentials_camelcase_not_auto_stage():
    body = json.dumps({
        "AccessKeyId": "AKIA...",
        "SecretAccessKey": "..."
    })
    out = classify_uploaded_json(body)
    assert out["kind"] == "aws_credentials"
    assert out["auto_stage_safe"] is False  # AWS prefers INI, not JSON


def test_classify_aws_credentials_snakecase():
    body = json.dumps({
        "aws_access_key_id": "AKIA...",
        "aws_secret_access_key": "...",
    })
    out = classify_uploaded_json(body)
    assert out["kind"] == "aws_credentials"


def test_classify_openai_api_key():
    body = json.dumps({"api_key": "sk-test-123"})
    out = classify_uploaded_json(body)
    assert out["kind"] == "openai_credentials"
    assert out["suggested_target"] is None  # belongs in .env, not as file
    assert out["auto_stage_safe"] is False


def test_classify_unknown_dict_falls_back_to_generic_config():
    out = classify_uploaded_json('{"foo": 1, "bar": 2, "baz": "x"}')
    assert out["kind"] == "generic_config_json"
    assert "foo" in out["summary"]


# ---- Text classifier ------------------------------------------------------


def test_classify_text_empty_returns_none():
    assert classify_uploaded_text("anything.txt", "") is None


def test_classify_markdown_by_extension():
    out = classify_uploaded_text("README.md", "# Hello")
    assert out["kind"] == "markdown_doc"


def test_classify_markdown_lowercase_extension():
    out = classify_uploaded_text("notes.markdown", "blah")
    assert out["kind"] == "markdown_doc"


def test_classify_requirements_txt():
    out = classify_uploaded_text(
        "requirements.txt", "pydantic>=2.7\nfastapi\n",
    )
    assert out["kind"] == "requirements_txt"


def test_classify_python_by_extension():
    out = classify_uploaded_text("foo.py", "def x(): return 1\n")
    assert out["kind"] == "python_script"


def test_classify_python_by_shebang_without_extension():
    src = "#!/usr/bin/env python\nimport sys\nprint(1)\n"
    out = classify_uploaded_text("script", src)
    assert out["kind"] == "python_script"


def test_classify_dotenv_by_extension():
    out = classify_uploaded_text(".env", "FOO=1\nBAR=2")
    assert out["kind"] == "dotenv_snippet"


def test_classify_dotenv_by_extension_variant():
    out = classify_uploaded_text(".env.example", "FOO=1\n")
    assert out["kind"] == "dotenv_snippet"


def test_classify_kv_majority_treated_as_dotenv():
    body = "# header comment\nFOO=1\nBAR=2\nBAZ=3\nQUX=4"
    out = classify_uploaded_text("anything.txt", body)
    assert out["kind"] == "dotenv_snippet"


def test_classify_random_text_returns_none():
    out = classify_uploaded_text("readme.txt", "Just some prose\nand more prose")
    assert out is None


# ---- Backwards-compat shim in messages.py ---------------------------------


def test_legacy_underscore_imports_still_work():
    from cascade.bot.handlers.messages import (
        _classify_uploaded_json,
        _classify_uploaded_text,
    )
    assert _classify_uploaded_json is classify_uploaded_json
    assert _classify_uploaded_text is classify_uploaded_text
