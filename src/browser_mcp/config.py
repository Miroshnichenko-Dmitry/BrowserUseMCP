"""Application settings, loaded from environment variables and an optional `.env` file."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Endpoint = Literal["browser", "agent"]

# Fixed MCP URLs. Clients connect to whichever toolset they need.
ENDPOINT_PATHS: dict[str, str] = {"browser": "/mcp/browser", "agent": "/mcp/agent"}


class LLMProvider(StrEnum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    # Any OpenAI-compatible endpoint: vLLM, LM Studio, OpenRouter, llama.cpp server, ...
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


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
    endpoints: Annotated[list[Endpoint], NoDecode] = Field(
        default_factory=lambda: ["browser", "agent"],
        description="Enabled MCP endpoints: browser (/mcp/browser), agent (/mcp/agent).",
    )
    stdio_endpoint: Endpoint = Field(
        default="browser", description="stdio carries a single server: which endpoint to expose."
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    auth_token: SecretStr | None = Field(
        default=None,
        description="If set, clients must send `Authorization: Bearer <token>` (all endpoints).",
    )
    browser_auth_token: SecretStr | None = Field(
        default=None, description="Overrides AUTH_TOKEN for /mcp/browser."
    )
    agent_auth_token: SecretStr | None = Field(
        default=None, description="Overrides AUTH_TOKEN for /mcp/agent."
    )

    # ------------------------------------------------------------------- llm
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_model: str = "qwen3.5:4b"
    llm_base_url: str | None = Field(
        default=None,
        description="Ollama host or OpenAI-compatible base URL. Defaults per provider.",
    )
    llm_api_key: SecretStr | None = None
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    llm_timeout_seconds: float = Field(default=120, gt=0)

    # Ollama defaults to a small context window; browser-use prompts are much larger.
    # Without this the model silently loses the system prompt and never calls `done`.
    ollama_num_ctx: int = Field(default=32768, ge=2048)
    ollama_think: bool | None = Field(default=None, description="Toggle reasoning for thinking models.")
    ollama_keep_alive: str | None = Field(default="30m", description="How long Ollama keeps the model.")

    # ----------------------------------------------------------------- agent
    agent_max_steps: int = Field(default=40, ge=1, le=500)
    agent_max_steps_limit: int = Field(default=150, ge=1, le=500)
    agent_max_failures: int = Field(default=3, ge=1)
    agent_max_actions_per_step: int = Field(default=3, ge=1, le=20)
    agent_step_timeout_seconds: int = Field(default=180, ge=10)
    agent_use_vision: bool | Literal["auto"] = True
    agent_use_judge: bool = Field(default=False, description="Extra LLM call that grades the run.")
    agent_flash_mode: bool = False
    agent_extend_system_message: str | None = None

    # --------------------------------------------------------------- browser
    max_browsers: int = Field(
        default=2, ge=1, le=32, description="Chromium instances alive at once, shared by both endpoints."
    )
    browser_headless: bool = False
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

    # ------------------------------------------- browser endpoint (sessions)
    browser_session_idle_seconds: float = Field(
        default=600, gt=0, description="Close a client's browser after this much inactivity."
    )
    browser_max_sessions_per_client: int = Field(default=3, ge=1, le=32)
    browser_open_wait_seconds: float = Field(
        default=15, ge=0, description="How long opening a session waits for a free browser."
    )
    browser_action_timeout_seconds: float = Field(default=60, gt=0)
    browser_state_max_chars: int = Field(
        default=12000, ge=1000, description="Cap on the page state returned after every action."
    )
    browser_read_max_chars: int = Field(default=30000, ge=1000)
    browser_screenshot_quality: int = Field(default=70, ge=10, le=100, description="JPEG quality.")

    # ----------------------------------------------------------------- tasks
    max_concurrent_tasks: int = Field(default=1, ge=1, le=32)
    max_queued_tasks: int = Field(default=10, ge=0)
    max_task_chars: int = Field(default=4000, ge=10)
    task_timeout_seconds: float = Field(default=900, gt=0)
    task_cancel_grace_seconds: float = Field(default=15, ge=0)
    task_result_ttl_seconds: float = Field(default=3600, gt=0)
    max_stored_tasks: int = Field(default=200, ge=1)
    wait_default_seconds: float = Field(
        default=50,
        ge=0,
        description="How long run/wait tools block before returning a 'running' snapshot. "
        "Keep below your MCP client's request timeout.",
    )
    wait_max_seconds: float = Field(default=300, ge=0)
    result_max_chars: int = Field(default=20000, ge=100)

    # ------------------------------------------------------------- artifacts
    artifacts_dir: str | None = Field(default=None, description="Save per-task history (and GIF) here.")
    artifacts_save_gif: bool = False

    @field_validator("browser_allowed_domains", "browser_prohibited_domains", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> list[str] | None:
        return _split_csv(value)

    @field_validator("endpoints", mode="before")
    @classmethod
    def _parse_endpoints(cls, value: object) -> list[str]:
        items = _split_csv(value) or []
        return list(dict.fromkeys(item.lower() for item in items))  # dedupe, keep order

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if not self.endpoints:
            raise ValueError("ENDPOINTS must enable at least one of: browser, agent")
        if self.transport == "stdio" and self.stdio_endpoint not in self.endpoints:
            raise ValueError(f"STDIO_ENDPOINT={self.stdio_endpoint} is not in ENDPOINTS")
        if not self.agent_enabled:
            return self  # LLM and agent settings are irrelevant without the agent endpoint
        if self.agent_max_steps > self.agent_max_steps_limit:
            raise ValueError("AGENT_MAX_STEPS must be <= AGENT_MAX_STEPS_LIMIT")
        if self.wait_default_seconds > self.wait_max_seconds:
            raise ValueError("WAIT_DEFAULT_SECONDS must be <= WAIT_MAX_SECONDS")
        if self.max_concurrent_tasks > self.max_browsers:
            raise ValueError("MAX_CONCURRENT_TASKS must be <= MAX_BROWSERS")
        needs_key = {LLMProvider.OPENAI, LLMProvider.ANTHROPIC, LLMProvider.GOOGLE}
        if self.llm_provider in needs_key and self.llm_api_key is None:
            raise ValueError(f"LLM_API_KEY is required for LLM_PROVIDER={self.llm_provider.value}")
        if self.llm_provider is LLMProvider.OPENAI_COMPATIBLE and not self.llm_base_url:
            raise ValueError("LLM_BASE_URL is required for LLM_PROVIDER=openai_compatible")
        return self

    @property
    def agent_enabled(self) -> bool:
        return "agent" in self.endpoints

    @property
    def browser_enabled(self) -> bool:
        return "browser" in self.endpoints

    def token_for(self, endpoint: Endpoint) -> SecretStr | None:
        specific = self.browser_auth_token if endpoint == "browser" else self.agent_auth_token
        return specific or self.auth_token

    @property
    def window_size(self) -> dict[str, int]:
        width, height = self.browser_window_size.split("x")
        return {"width": int(width), "height": int(height)}

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    @property
    def effective_llm_base_url(self) -> str | None:
        if self.llm_base_url:
            return self.llm_base_url.rstrip("/")
        if self.llm_provider is LLMProvider.OLLAMA:
            return "http://localhost:11434"
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
