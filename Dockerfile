# syntax=docker/dockerfile:1.7

# Multi-stage build. The builder layer compiles the wheel; the runtime layer
# only carries Python + the installed package, which keeps the final image
# small and avoids shipping uv into production.

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy lockfile + manifest first so `uv sync` is cached across rebuilds when
# only application code changes.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_LEVEL=INFO

# Run as an unprivileged user.
RUN groupadd --system app && useradd --system --gid app --home /home/app --create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/healthz', timeout=3).status==200 else sys.exit(1)" \
    || exit 1

ENTRYPOINT ["pexels-mcp-server"]
