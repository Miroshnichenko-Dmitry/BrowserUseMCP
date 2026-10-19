"""Application settings, loaded from environment variables and an optional `.env` file."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Fixed MCP URL.
MCP_PATH = "/mcp/browser"

MIN_TOKEN_LENGTH = 16
_AGENT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

CsvList = Annotated[list[str] | None, NoDecode]


def _split_csv(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        items = [item for item in items if item]
        return items or None
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()] or None
    raise TypeError("expected a comma-separated string or a list")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- server
    transport: Literal["http", "stdio"] = "http"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    auth_token: SecretStr | None = Field(
        default=None,
        description="If set, clients must send `Authorization: Bearer <token>`.",
    )
    auth_tokens: Annotated[dict[str, SecretStr], NoDecode] = Field(
        default_factory=dict,
        description="Per-agent tokens as `name:token,name2:token2`. The server tells agents apart by "
        "their token, which stateless MCP clients need for the browser handover to work.",
    )

    # --------------------------------------------------------------- browser
    browser_headless: bool = True
    browser_window_size: str = Field(
        default="1280x900",
        pattern=r"^\d{3,4}x\d{3,4}$",
        description="Viewport (headless) or window size. Defaults to the host screen otherwise, "
        "which makes screenshots and click coordinates huge on wide monitors.",
    )
    browser_executable_path: str | None = None
    browser_proxy: str | None = Field(
        default=None,
        description="Proxy for Chromium traffic, e.g. http://http.docker.internal:3128 or socks5://host:1080.",
    )
    browser_proxy_bypass: str | None = Field(default=None, description="Comma-separated hosts to bypass.")
    browser_allowed_domains: CsvList = None
    browser_prohibited_domains: CsvList = None
    browser_block_ip_addresses: bool = False
    browser_enable_extensions: bool = Field(
        default=False,
        description="uBlock/cookie extensions; downloaded at runtime, so off by default.",
    )

    # ------------------------------------------------- one agent at a time
    browser_handover_idle_seconds: float = Field(
        default=30,
        ge=0,
        description="A waiting agent takes the browser over once the current agent has been idle this "
        "long. Nobody waiting: the browser stays with the current agent, however long it is idle.",
    )
    browser_busy_wait_seconds: float = Field(
        default=20,
        ge=0,
        description="How long a call from another agent waits for the browser before failing as busy.",
    )
    browser_session_max_seconds: float = Field(
        default=1800,
        ge=0,
        description="After this, a waiting agent takes the browser over even from an active agent "
        "(as soon as its current call ends). 0 = no limit.",
    )

    # ----------------------------------------------------------------- tools
    browser_action_timeout_seconds: float = Field(default=60, gt=0)
    browser_state_max_chars: int = Field(
        default=12000, ge=1000, description="Cap on the page state returned after every action."
    )
    browser_read_max_chars: int = Field(default=30000, ge=1000)
    browser_screenshot_quality: int = Field(default=70, ge=10, le=100, description="JPEG quality.")

    @field_validator("browser_allowed_domains", "browser_prohibited_domains", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> list[str] | None:
        return _split_csv(value)

    @field_validator("auth_tokens", mode="before")
    @classmethod
    def _parse_auth_tokens(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        tokens: dict[str, str] = {}
        for item in _split_csv(value) or []:
            name, sep, token = item.partition(":")
            if not sep:
                raise ValueError("AUTH_TOKENS entries must look like name:token")
            name = name.strip()
            if name in tokens:
                raise ValueError(f"AUTH_TOKENS: duplicate agent name {name!r}")
            tokens[name] = token.strip()
        return tokens

    @model_validator(mode="after")
    def _check_tokens(self) -> Settings:
        for name in self.auth_tokens:
            if not _AGENT_NAME.match(name):
                raise ValueError(f"AUTH_TOKENS: agent name {name!r} may only use letters, digits, '_.-'")
        values = [t.get_secret_value() for t in self.auth_tokens.values()]
        if self.auth_token is not None:
            values.append(self.auth_token.get_secret_value())
        if any(len(v) < MIN_TOKEN_LENGTH for v in values):
            raise ValueError(f"Auth tokens must be at least {MIN_TOKEN_LENGTH} characters")
        if len(set(values)) != len(values):
            raise ValueError("Auth tokens must be unique")
        return self

    @property
    def token_map(self) -> dict[str, str | None]:
        """Every accepted token -> agent name (None for the shared AUTH_TOKEN)."""
        tokens: dict[str, str | None] = {
            token.get_secret_value(): name for name, token in self.auth_tokens.items()
        }
        if self.auth_token is not None:
            tokens[self.auth_token.get_secret_value()] = None
        return tokens

    @property
    def window_size(self) -> dict[str, int]:
        width, height = self.browser_window_size.split("x")
        return {"width": int(width), "height": int(height)}

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
