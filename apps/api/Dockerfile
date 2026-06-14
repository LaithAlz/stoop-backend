# syntax=docker/dockerfile:1
#
# Multi-stage build for the Stoop API.
#   builder — installs production dependencies into a venv with uv
#   final   — minimal non-root runtime, copies only the venv + app source
#
# Build context is apps/api (this directory). Used by Fly.io (#13) and for
# local parity testing. Local dev should run uvicorn via uv directly.

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# UV_COMPILE_BYTECODE — ship .pyc so the runtime doesn't compile on first hit.
# UV_LINK_MODE=copy   — avoid hardlink warnings across the build cache mount.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy only the dependency manifests first so the (slow) dependency layer is
# cached until pyproject.toml / uv.lock actually change.
COPY pyproject.toml uv.lock ./

# Install production deps only (no dev group) and not the project itself —
# the app is run from source, not installed as a package.
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Final runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root user — smaller blast radius if the process is compromised.
RUN useradd -m -u 1000 app

WORKDIR /app

# Bring in the resolved venv from the builder, then the app source. Source is
# copied last so code changes don't invalidate the dependency layer.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app

USER app

EXPOSE 8080

# Fly expects 8080. Bind all interfaces so the container is reachable.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
