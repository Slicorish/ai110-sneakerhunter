from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_client import GeminiClient


def _client(monkeypatch, temperature=0.7):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = GeminiClient(temperature=temperature)
    client.client = MagicMock()
    return client


def test_sends_system_prompt_temperature_and_json_mode(monkeypatch):
    client = _client(monkeypatch)
    client.client.models.generate_content.return_value = SimpleNamespace(text='{"ok": true}')

    assert client.complete("SYS", "USER") == '{"ok": true}'

    kwargs = client.client.models.generate_content.call_args.kwargs
    assert kwargs["contents"] == "USER"
    assert kwargs["config"].system_instruction == "SYS"
    assert kwargs["config"].temperature == 0.7
    assert kwargs["config"].response_mime_type == "application/json"


def test_api_errors_are_raised_not_swallowed(monkeypatch):
    client = _client(monkeypatch)
    client.client.models.generate_content.side_effect = RuntimeError("429 quota")
    with pytest.raises(RuntimeError, match="429"):
        client.complete("SYS", "USER")


def test_blocked_response_returns_empty_string(monkeypatch):
    client = _client(monkeypatch)
    client.client.models.generate_content.return_value = SimpleNamespace(text=None)
    assert client.complete("SYS", "USER") == ""


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError, match="Missing GEMINI_API_KEY"):
        GeminiClient()
