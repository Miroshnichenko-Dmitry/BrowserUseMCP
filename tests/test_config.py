import pytest
from pydantic import ValidationError

from browser_mcp.config import Settings


def make(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_defaults_for_one_agent_at_a_time():
    s = make()
    assert s.browser_headless is True
    assert s.browser_handover_idle_seconds == 60
    assert s.browser_busy_wait_seconds == 20
    assert s.browser_session_max_seconds == 1800
    assert s.token_map == {}


def test_warns_when_agents_cannot_be_told_apart():
    agents = f"pix-1:{'a' * 20},pix-2:{'b' * 20}"
    assert "AUTH_TOKENS is not set" in make().identity_warning()
    assert (
        "AUTH_TOKEN is set next to AUTH_TOKENS"
        in make(auth_tokens=agents, auth_token="s" * 20).identity_warning()
    )
    assert make(auth_tokens=agents).identity_warning() is None


def test_csv_domains(monkeypatch):
    monkeypatch.setenv("BROWSER_ALLOWED_DOMAINS", "*.wikipedia.org, example.com ,")
    s = Settings(_env_file=None)
    assert s.browser_allowed_domains == ["*.wikipedia.org", "example.com"]


def test_agent_tokens_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_TOKENS", "pix-1:" + "a" * 20 + ", pix-2:" + "b" * 20)
    monkeypatch.setenv("AUTH_TOKEN", "s" * 20)
    s = Settings(_env_file=None)
    assert s.token_map == {"a" * 20: "pix-1", "b" * 20: "pix-2", "s" * 20: None}


@pytest.mark.parametrize(
    "value",
    [
        "pix-1",  # no token
        "pix-1:short",  # too short
        f"pix 1:{'a' * 20}",  # bad name
        f"pix-1:{'a' * 20},pix-1:{'b' * 20}",  # duplicate name
        f"pix-1:{'a' * 20},pix-2:{'a' * 20}",  # duplicate token
    ],
)
def test_bad_agent_tokens(value):
    with pytest.raises(ValidationError):
        make(auth_tokens=value)


def test_shared_token_must_be_long_and_differ_from_agent_tokens():
    with pytest.raises(ValidationError):
        make(auth_token="short")
    with pytest.raises(ValidationError):
        make(auth_token="a" * 20, auth_tokens=f"pix-1:{'a' * 20}")


def test_window_size():
    assert make().window_size == {"width": 1280, "height": 900}
    assert make(browser_window_size="1920x1080").window_size == {"width": 1920, "height": 1080}
    with pytest.raises(ValidationError):
        make(browser_window_size="big")


def test_browser_proxy_goes_into_profile(tmp_path):
    from browser_mcp.driver import build_profile

    s = make(browser_proxy="http://proxy:3128", browser_proxy_bypass="localhost")
    proxy = build_profile(s, tmp_path).proxy
    assert proxy.server == "http://proxy:3128" and proxy.bypass == "localhost"
    assert build_profile(make(), tmp_path).proxy is None


def test_server_policy_goes_into_profile(tmp_path):
    from browser_mcp.driver import build_profile

    s = make(browser_allowed_domains="example.com", browser_block_ip_addresses="true")
    profile = build_profile(s, tmp_path)
    assert list(profile.allowed_domains) == ["example.com"]
    assert profile.block_ip_addresses and profile.headless and profile.keep_alive
