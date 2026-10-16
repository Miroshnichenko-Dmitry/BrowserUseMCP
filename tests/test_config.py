import pytest
from pydantic import ValidationError

from browser_mcp.config import LLMProvider, Settings


def make(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_defaults_keep_current_local_model():
    s = make()
    assert s.llm_provider is LLMProvider.OLLAMA
    assert s.llm_model == "qwen3.5:4b"
    assert s.effective_llm_base_url == "http://localhost:11434"
    assert s.ollama_num_ctx >= 16384


def test_csv_domains(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "*.wikipedia.org, example.com ,")
    s = Settings(_env_file=None)
    assert s.browser_allowed_domains == ["*.wikipedia.org", "example.com"]


def test_cloud_provider_requires_key():
    with pytest.raises(ValidationError):
        make(llm_provider="openai", llm_model="gpt-5-mini")


def test_openai_compatible_requires_base_url():
    with pytest.raises(ValidationError):
        make(llm_provider="openai_compatible", llm_model="qwen3.5:32b")
    s = make(llm_provider="openai_compatible", llm_model="m", llm_base_url="http://vllm:8000/v1/")
    assert s.effective_llm_base_url == "http://vllm:8000/v1"


def test_vision_auto_and_bool():
    assert make(agent_use_vision="auto").agent_use_vision == "auto"
    assert make(agent_use_vision="false").agent_use_vision is False


def test_step_limits_consistent():
    with pytest.raises(ValidationError):
        make(agent_max_steps=200, agent_max_steps_limit=100)
