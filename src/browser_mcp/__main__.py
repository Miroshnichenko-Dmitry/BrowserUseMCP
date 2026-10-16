"""Entry point: `browser-mcp` or `python -m browser_mcp`."""

from __future__ import annotations

import logging
import sys

from .logging_setup import configure_logging, prepare_environment


def main() -> None:
    prepare_environment()  # before anything imports browser_use

    from pydantic import ValidationError

    from .config import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        sys.exit(2)

    configure_logging(settings.log_level, settings.log_format)
    logger = logging.getLogger("browser_mcp")

    from .runner import BrowserAgentRunner
    from .server import create_server

    if settings.transport == "http" and not settings.is_loopback and settings.auth_token is None:
        logger.warning(
            "Listening on %s without AUTH_TOKEN. Anyone who can reach the port controls the browser. "
            "Bind the published port to 127.0.0.1 or set AUTH_TOKEN.",
            settings.host,
        )

    mcp = create_server(settings, BrowserAgentRunner(settings))
    logger.info(
        "starting server",
        extra={
            "transport": settings.transport,
            "address": f"{settings.host}:{settings.port}{settings.mcp_path}",
            "llm": f"{settings.llm_provider.value}:{settings.llm_model}",
            "max_concurrent": settings.max_concurrent_tasks,
        },
    )
    if settings.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            path=settings.mcp_path,
            show_banner=False,
            log_level=settings.log_level.lower(),
            # Keep our logging config (uvicorn would install its own) and skip per-request
            # access logs: health checks would flood them.
            uvicorn_config={"log_config": None, "access_log": False, "timeout_graceful_shutdown": 5},
        )


if __name__ == "__main__":
    main()
