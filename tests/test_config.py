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


def test_endpoints_csv_and_defaults():
    assert make().endpoints == ["browser", "agent"]
    s = make(endpoints="Agent, browser,agent")
    assert s.endpoints == ["agent", "browser"]
    with pytest.raises(ValidationError):
        make(endpoints="browser,shell")
    with pytest.raises(ValidationError):
        make(endpoints="")


def test_llm_settings_ignored_without_agent():
    # A cloud provider without a key is only an error when the agent endpoint is enabled.
    s = make(endpoints="browser", llm_provider="openai", llm_model="gpt-5-mini")
    assert not s.agent_enabled and s.browser_enabled


def test_agent_concurrency_fits_in_browser_pool():
    with pytest.raises(ValidationError):
        make(max_browsers=1, max_concurrent_tasks=2)


def test_stdio_endpoint_must_be_enabled():
    with pytest.raises(ValidationError):
        make(transport="stdio", endpoints="agent", stdio_endpoint="browser")


def test_per_endpoint_token_overrides_global():
    s = make(auth_token="g" * 20, agent_auth_token="a" * 20)
    assert s.token_for("browser").get_secret_value() == "g" * 20
    assert s.token_for("agent").get_secret_value() == "a" * 20


def test_window_size():
    assert make().window_size == {"width": 1280, "height": 900}
    assert make(browser_window_size="1920x1080").window_size == {"width": 1920, "height": 1080}
    with pytest.raises(ValidationError):
        make(browser_window_size="big")


def test_browser_proxy_goes_into_profile(tmp_path):
    from browser_mcp.browsers import build_profile

    s = make(browser_proxy="http://proxy:3128", browser_proxy_bypass="localhost")
    proxy = build_profile(s, tmp_path).proxy
    assert proxy.server == "http://proxy:3128" and proxy.bypass == "localhost"
    assert build_profile(make(), tmp_path).proxy is None
