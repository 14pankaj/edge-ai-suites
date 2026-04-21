# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# ---------------------------------------------------------------------------
# Stage 1 — dependency installer (uses uv for fast, reproducible installs)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2 — lean runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=9000 \
    SNAPSHOT_DIR=/app/snapshots

# System deps: libGL for OpenCV, libglib for GStreamer backend
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m appuser

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application source
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser resources/tools.json ./resources/tools.json
RUN mkdir -p /app/snapshots /app/config /app/resources \
    && chown -R appuser:appuser /app/snapshots /app/config /app/resources

USER appuser
EXPOSE 9000

CMD ["python", "-m", "src.main"]
