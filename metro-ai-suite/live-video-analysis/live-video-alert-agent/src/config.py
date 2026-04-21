# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Application configuration.

All values are read from environment variables with sensible defaults.
Refer to docs/user-guide/configuration.md for the full reference.
"""

import os
import logging


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key, "")
    if not val:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


class Settings:
    PORT: int = _int("PORT", 9000)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    RTSP_URL: str = os.getenv("RTSP_URL", "")
    VLM_URL: str = os.getenv("VLM_URL", "http://localhost:8000/v3")
    MODEL_NAME: str = os.getenv("MODEL_NAME", "Phi-3.5-Vision")
    VLM_IMAGE_MAX_DIM: int = _int("VLM_IMAGE_MAX_DIM", 448)
    VLM_JPEG_QUALITY: int = _int("VLM_JPEG_QUALITY", 70)
    VLM_TIMEOUT: float = _float("VLM_TIMEOUT", 30.0)
    VLM_MAX_RETRIES: int = _int("VLM_MAX_RETRIES", 2)
    VLM_MAX_TOKENS: int = _int("VLM_MAX_TOKENS", 150)
    VLM_MAX_CONCURRENCY: int = _int("VLM_MAX_CONCURRENCY", 2)

    ANALYSIS_INTERVAL: float = _float("ANALYSIS_INTERVAL", 2.0)
    FRAME_BUFFER_SIZE: int = _int("FRAME_BUFFER_SIZE", 10)
    CAPTURE_FPS: float = _float("CAPTURE_FPS", 0)  # 0 = auto (1/ANALYSIS_INTERVAL)
    CAPTURE_RESIZE_HEIGHT: int = _int("CAPTURE_RESIZE_HEIGHT", 720)

    # ADK uses local OVMS endpoint (LOCAL_LLM_URL) for tool dispatch.
    USE_ADK: bool = _bool("USE_ADK", True)

    LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://ovms:8000/v3")
    LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "Phi-4-mini-instruct")
    LOCAL_LLM_TIMEOUT: float = _float("LOCAL_LLM_TIMEOUT", 30.0)

    ALERT_HISTORY_SIZE: int = _int("ALERT_HISTORY_SIZE", 500)

    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _int("SMTP_PORT", 587)
    SMTP_USE_TLS: bool = _bool("SMTP_USE_TLS", True)
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    ALERT_EMAIL_FROM: str = os.getenv("ALERT_EMAIL_FROM", "")
    ALERT_EMAIL_TO: str = os.getenv("ALERT_EMAIL_TO", "")

    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    # HMAC-SHA256 secret to sign webhook payloads (optional)
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

    MQTT_BROKER: str = os.getenv("MQTT_BROKER", "")
    MQTT_PORT: int = _int("MQTT_PORT", 1883)
    MQTT_USERNAME: str = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD: str = os.getenv("MQTT_PASSWORD", "")
    MQTT_BASE_TOPIC: str = os.getenv("MQTT_BASE_TOPIC", "live-video-alerts")

    SNAPSHOT_DIR: str = os.getenv("SNAPSHOT_DIR", "snapshots")

    MCP_ENABLED: bool = _bool("MCP_ENABLED", True)
    MCP_CONFIG_FILE: str = os.getenv("MCP_CONFIG_FILE", "resources/mcp_servers.json")


settings = Settings()


def setup_logging():
    """Configure structured logging for production."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "multipart", "uvicorn.access", "paho"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
