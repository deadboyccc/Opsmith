# syntax=docker/dockerfile:1.7
# Opsmith container image. Targets:
#   runtime (default) - API / worker / scheduler processes
#   dev               - adds the dev dependency group (pytest, ruff, mypy) for CI and local testing
FROM ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# git is required by the audit runner (incremental diffs) and the executor (branch + commit).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The Docker CLI only (no daemon): the worker uses it to start sandbox containers when it is
# given a Docker socket (docker-compose.sandbox.yml). Without a socket it is inert.
FROM docker:29.9.3-cli AS docker-cli

FROM base AS runtime
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --extra anthropic --extra postgres
COPY src ./src
RUN uv sync --frozen --no-dev --extra anthropic --extra postgres \
    && useradd --create-home --uid 10001 opsmith \
    && mkdir -p /data && chown opsmith /data
USER opsmith
ENV OPSMITH_DATA_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["opsmith", "api", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS dev
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --all-extras --no-install-project
COPY . .
RUN uv sync --frozen --all-extras \
    && git config --global user.email "dev@opsmith.local" \
    && git config --global user.name "opsmith-dev" \
    && git config --global init.defaultBranch main \
    && git config --global --add safe.directory '*'
CMD ["pytest"]
