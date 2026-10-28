from __future__ import annotations

import os

import pytest

from changeguard.github.service import _analysis_service
from changeguard.llm import GeminiLLMAdapter, OllamaLLMAdapter, create_llm_adapter, default_llm_provider, load_local_env, max_tool_calls_for
from changeguard.storage import SQLiteAnalysisStore

_KEYS = ("CG_PLAIN", "CG_DQ", "CG_SQ", "CG_EXPORT", "CG_COMMENT", "CG_HASH_IN_QUOTES", "CG_EXISTING", "CG_PARENT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (*_KEYS, "CHANGEGUARD_ENV_FILE", "CHANGEGUARD_LLM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _KEYS:
        os.environ.pop(key, None)


def test_env_file_parsing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\n"
        "CG_PLAIN=value\n"
        'CG_DQ="quoted value"\n'
        "CG_SQ='single'\n"
        "export CG_EXPORT=exported\n"
        "CG_COMMENT=abc # trailing comment\n"
        'CG_HASH_IN_QUOTES="a # b"\n'
        "CG_EXISTING=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CG_EXISTING", "from-env")
    load_local_env(env)
    assert os.environ["CG_PLAIN"] == "value"
    assert os.environ["CG_DQ"] == "quoted value"
    assert os.environ["CG_SQ"] == "single"
    assert os.environ["CG_EXPORT"] == "exported"
    assert os.environ["CG_COMMENT"] == "abc"
    assert os.environ["CG_HASH_IN_QUOTES"] == "a # b"
    assert os.environ["CG_EXISTING"] == "from-env"


def test_env_file_found_in_parent_directory(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("CG_PARENT=yes\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    load_local_env()
    assert os.environ["CG_PARENT"] == "yes"


def test_env_file_search_stops_at_git_root(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CG_PARENT=outside\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    load_local_env()
    assert "CG_PARENT" not in os.environ


def test_env_file_override_path(tmp_path, monkeypatch):
    custom = tmp_path / "custom.env"
    custom.write_text("CG_PLAIN=custom\n", encoding="utf-8")
    monkeypatch.setenv("CHANGEGUARD_ENV_FILE", str(custom))
    monkeypatch.chdir(tmp_path)
    load_local_env()
    assert os.environ["CG_PLAIN"] == "custom"


def test_default_llm_provider(monkeypatch):
    assert default_llm_provider() == "ollama"
    monkeypatch.setenv("CHANGEGUARD_LLM_PROVIDER", " Gemini ")
    assert default_llm_provider() == "gemini"
    monkeypatch.setenv("CHANGEGUARD_LLM_PROVIDER", "openai")
    with pytest.raises(ValueError):
        default_llm_provider()


def test_provider_adapters_and_tool_cap(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert isinstance(create_llm_adapter("gemini"), GeminiLLMAdapter)
    assert isinstance(create_llm_adapter("ollama"), OllamaLLMAdapter)
    with pytest.raises(ValueError):
        create_llm_adapter("other")
    assert max_tool_calls_for("gemini", 5) == 1
    assert max_tool_calls_for("ollama", 5) == 5


def test_pr_analysis_service_uses_selected_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    store = SQLiteAnalysisStore(str(tmp_path / "a.sqlite3"))
    service = _analysis_service(store, True, str(tmp_path / "a.sqlite3"), llm_provider="gemini")
    assert isinstance(service.investigator.llm, GeminiLLMAdapter)
    assert service.investigator.max_tool_calls == 1
