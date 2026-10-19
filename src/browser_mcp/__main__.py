"""Entry point: `browser-mcp` or `python -m browser_mcp`."""

from __future__ import annotations

import logging
import sys

from .logging_setup import configure_logging, prepare_environment


def main() -> None:
    prepare_environment()  # before anything imports browser_use

    from pydantic import ValidationError

    from .config import ENDPOINT_PATHS, get_settings

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
        # stdio carries exactly one MCP server.
        logger.info("starting server", extra={"transport": "stdio", "endpoint": settings.stdio_endpoint})
        components.servers[settings.stdio_endpoint].run(transport="stdio", show_banner=False)
        return

    if not settings.is_loopback:
        for name in components.servers:
            if settings.token_for(name) is None:
                logger.warning(
                    "Listening on %s without a token for %s. Anyone who can reach the port controls the "
                    "browser. Bind the published port to 127.0.0.1 or set AUTH_TOKEN.",
                    settings.host,
                    ENDPOINT_PATHS[name],
                )

    logger.info(
        "starting server",
        extra={
            "transport": "http",
            "endpoints": ", ".join(
                f"http://{settings.host}:{settings.port}{ENDPOINT_PATHS[n]}" for n in components.servers
            ),
            "max_browsers": settings.max_browsers,
            **(
                {"llm": f"{settings.llm_provider.value}:{settings.llm_model}"}
                if settings.agent_enabled
                else {}
            ),
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
