# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG DEBIAN_RELEASE=trixie
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached layer), project code second.
# uv.lock is optional: with it the build is fully reproducible (--frozen).
COPY pyproject.toml uv.loc[k] ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then FROZEN=--frozen; else FROZEN=; fi; \
    uv sync $FROZEN --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then FROZEN=--frozen; else FROZEN=; fi; \
    uv sync $FROZEN --no-dev --no-editable

# ------------------------------------------------------------------- test
# docker build --target test -t browser-use-mcp:test . && docker run --rm browser-use-mcp:test
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then FROZEN=--frozen; else FROZEN=; fi; \
    uv sync $FROZEN
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "ruff check src tests && pytest -q"]

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_RELEASE} AS runtime

# Chromium from Debian (security updates via apt, works on amd64 and arm64),
# fonts for Cyrillic/CJK/emoji pages, tini as PID 1 to reap Chromium zombies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium \
      fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji \
      tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
 && useradd --system --uid ${APP_UID} --gid app --create-home --home-dir /home/app app \
 && mkdir -p /data/artifacts \
 && chown -R app:app /data

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    IN_DOCKER=true \
    TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8000 \
    BROWSER_HEADLESS=true \
    BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    BROWSER_USE_CONFIG_DIR=/home/app/.config/browseruse \
    LOG_FORMAT=json

USER app
WORKDIR /home/app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["browser-mcp"]

LABEL org.opencontainers.image.title="browser-use-mcp" \
      org.opencontainers.image.description="MCP server running browser-use agents as background tasks" \
      org.opencontainers.image.version="1.1.0"
