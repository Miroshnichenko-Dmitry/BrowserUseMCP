"""Application settings, loaded from environment variables and an optional `.env` file."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    mcp_path: str = "/mcp"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    auth_token: SecretStr | None = Field(
        default=None,
        description="If set, clients must send `Authorization: Bearer <token>`.",
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
    browser_headless: bool = True
    browser_executable_path: str | None = None
    browser_allowed_domains: CsvList = None
    browser_prohibited_domains: CsvList = None
    browser_block_ip_addresses: bool = False
    browser_enable_extensions: bool = Field(
        default=False,
        description="uBlock/cookie extensions; downloaded at runtime, so off by default.",
    )

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

    @field_validator("mcp_path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return "/" + value.strip("/")

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.agent_max_steps > self.agent_max_steps_limit:
            raise ValueError("AGENT_MAX_STEPS must be <= AGENT_MAX_STEPS_LIMIT")
        if self.wait_default_seconds > self.wait_max_seconds:
            raise ValueError("WAIT_DEFAULT_SECONDS must be <= WAIT_MAX_SECONDS")
        needs_key = {LLMProvider.OPENAI, LLMProvider.ANTHROPIC, LLMProvider.GOOGLE}
        if self.llm_provider in needs_key and self.llm_api_key is None:
            raise ValueError(f"LLM_API_KEY is required for LLM_PROVIDER={self.llm_provider.value}")
        if self.llm_provider is LLMProvider.OPENAI_COMPATIBLE and not self.llm_base_url:
            raise ValueError("LLM_BASE_URL is required for LLM_PROVIDER=openai_compatible")
        return self

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
