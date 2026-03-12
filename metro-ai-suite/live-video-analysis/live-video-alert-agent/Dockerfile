# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

# ---------------------------------------------------------------------------
# Stage 1 — dependency installer (uses uv for fast, reproducible installs)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — lean runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application source and static resources
COPY --chown=appuser:appuser src/ ./src/

# Pre-create runtime directories so appuser can write to them even when
# no external volume is mounted (snapshots, alert config persisted to disk)
RUN mkdir -p /app/snapshots /app/config \
    && chown -R appuser:appuser /app/snapshots /app/config

USER appuser
# EXPOSE is documentation; actual binding is controlled by docker-compose PORT var
EXPOSE 9000

# Liveness probe — relies on the /health endpoint added in this rewrite
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-9000}/health || exit 1

CMD ["python", "src/main.py"]
