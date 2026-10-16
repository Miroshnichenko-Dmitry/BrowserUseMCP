"""LLM factory and readiness checks. Switch models via env vars, no code changes needed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from .config import LLMProvider, Settings

if TYPE_CHECKING:
    from browser_use.llm.base import BaseChatModel

logger = logging.getLogger(__name__)


def _secret(settings: Settings) -> str | None:
    return settings.llm_api_key.get_secret_value() if settings.llm_api_key else None


def build_llm(settings: Settings) -> BaseChatModel:
    provider = settings.llm_provider
    common: dict[str, Any] = {"model": settings.llm_model}

    if provider is LLMProvider.OLLAMA:
        from browser_use.llm.ollama.chat import ChatOllama

        options: dict[str, Any] = {"num_ctx": settings.ollama_num_ctx}
        if settings.llm_temperature is not None:
            options["temperature"] = settings.llm_temperature
        if settings.ollama_think is not None:
            options["think"] = settings.ollama_think
        if settings.ollama_keep_alive:
            options["keep_alive"] = settings.ollama_keep_alive
        return ChatOllama(
            **common,
            host=settings.effective_llm_base_url,
            timeout=settings.llm_timeout_seconds,
            ollama_options=options,
        )

    if provider in (LLMProvider.OPENAI, LLMProvider.OPENAI_COMPATIBLE):
        from browser_use.llm.openai.chat import ChatOpenAI

        kwargs: dict[str, Any] = {
            **common,
            "api_key": _secret(settings) or "not-needed",
            "base_url": settings.effective_llm_base_url,
            "timeout": settings.llm_timeout_seconds,
        }
        if settings.llm_temperature is not None:
            kwargs["temperature"] = settings.llm_temperature
        return ChatOpenAI(**kwargs)

    if provider is LLMProvider.ANTHROPIC:
        from browser_use.llm.anthropic.chat import ChatAnthropic

        kwargs = {**common, "api_key": _secret(settings), "timeout": settings.llm_timeout_seconds}
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        if settings.llm_temperature is not None:
            kwargs["temperature"] = settings.llm_temperature
        return ChatAnthropic(**kwargs)

    if provider is LLMProvider.GOOGLE:
        from browser_use.llm.google.chat import ChatGoogle

        kwargs = {**common, "api_key": _secret(settings)}
        if settings.llm_temperature is not None:
            kwargs["temperature"] = settings.llm_temperature
        return ChatGoogle(**kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


async def check_llm_ready(settings: Settings, timeout: float = 5) -> tuple[bool, str]:
    """Cheap readiness probe. For Ollama also verifies that the model is pulled."""
    if settings.llm_provider is not LLMProvider.OLLAMA:
        # Cloud providers: we do not spend tokens on a probe; config validation already ran.
        return True, f"{settings.llm_provider.value}:{settings.llm_model} (not probed)"

    base = settings.effective_llm_base_url
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base}/api/tags")
            response.raise_for_status()
            models = {m.get("name") for m in response.json().get("models", [])}
    except Exception as exc:
        return False, f"Ollama is unreachable at {base}: {type(exc).__name__}: {exc}"

    wanted = settings.llm_model
    candidates = {wanted, f"{wanted}:latest"} if ":" not in wanted else {wanted}
    if not candidates & models:
        return False, f"Model '{wanted}' is not pulled in Ollama. Run: ollama pull {wanted}"
    return True, f"ollama:{wanted} at {base}"
