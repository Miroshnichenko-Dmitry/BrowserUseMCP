"""Entry point: `browser-mcp` or `python -m browser_mcp`."""

from __future__ import annotations

import logging
import sys

from .logging_setup import configure_logging, prepare_environment


def main() -> None:
    prepare_environment()  # before anything imports browser_use

    from pydantic import ValidationError

    from .config import MCP_PATH, get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        sys.exit(2)

    configure_logging(settings.log_level, settings.log_format)
    logger = logging.getLogger("browser_mcp")

    from .app import build_components, create_app

    components = build_components(settings)

    if settings.transport == "stdio":
        logger.info("starting server", extra={"transport": "stdio"})
        components.server.run(transport="stdio", show_banner=False)
        return

    if not settings.is_loopback and not settings.token_map:
        logger.warning(
            "Listening on %s without AUTH_TOKEN / AUTH_TOKENS. Anyone who can reach the port controls "
            "the browser. Bind the published port to 127.0.0.1 or set a token.",
            settings.host,
        )

    logger.info(
        "starting server",
        extra={
            "transport": "http",
            "endpoint": f"http://{settings.host}:{settings.port}{MCP_PATH}",
            "handover_idle_seconds": settings.browser_handover_idle_seconds,
            "agent_tokens": len(settings.auth_tokens),
        },
    )

    import uvicorn

    uvicorn.run(
        create_app(components),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Keep our logging config (uvicorn would install its own) and skip per-request
        # access logs: health checks would flood them.
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
