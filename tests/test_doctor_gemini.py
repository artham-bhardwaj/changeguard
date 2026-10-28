from __future__ import annotations

import io
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from changeguard import project
from changeguard.llm.config import env_source, load_local_env
from changeguard.llm.gemini import check_api_key


def _http_error(code: int, body: bytes = b'{"error": {"message": "API key not valid."}}') -> HTTPError:
    return HTTPError("https://example.test", code, "error", Message(), io.BytesIO(body))


def _opener(result):
    def opener(request, timeout):
        assert request.get_method() == "GET"
        assert request.get_header("X-goog-api-key") == "secret-key"
        assert "secret-key" not in request.full_url
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(b"{}")
    return opener


@pytest.mark.parametrize(
    ("result", "ok", "text"),
    [
        (None, True, "Gemini key works (m)"),
        (_http_error(400), False, "Gemini rejected the key (HTTP 400): API key not valid."),
        (_http_error(404), False, "model 'm' was not found"),
        (_http_error(429), True, "rate limit"),
        (URLError("offline"), False, "could not reach Gemini"),
    ],
)
def test_check_api_key(result, ok, text):
    got_ok, detail = check_api_key("secret-key", "m", opener=_opener(result))
    assert got_ok is ok
    assert text in detail


def test_doctor_reports_key_source_and_caches(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(project, "_GEMINI_STATUS", {})
    load_local_env(env_file)
    calls = []

    def fake_check(key, model):
        calls.append(key)
        return False, "Gemini rejected the key (HTTP 400): bad"

    ok, detail = project.gemini_key_status(check=fake_check)
    assert not ok
    assert detail.endswith(f"key from {env_file}")
    assert env_source("GEMINI_API_KEY") == str(env_file)
    assert calls == ["from-file"]
    monkeypatch.delenv("GEMINI_API_KEY")


def test_doctor_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert project.gemini_key_status() == (False, "Gemini selected but GEMINI_API_KEY is not set")


def test_environment_variable_source(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "x")
    assert env_source("SOME_SETTING") == "environment"
    assert env_source("NOT_SET_ANYWHERE_123") is None
