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
    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    PORT: int = _int("PORT", 9000)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # API security — set a non-empty value to enable bearer-token auth
    API_KEY: str = os.getenv("API_KEY", "")

    # ------------------------------------------------------------------ #
    # VLM / OVMS
    # ------------------------------------------------------------------ #
    RTSP_URL: str = os.getenv("RTSP_URL", "")
    VLM_URL: str = os.getenv("VLM_URL", "http://localhost:8000/v3")
    VLM_API_KEY: str = os.getenv("VLM_API_KEY", "dummy")
    MODEL_NAME: str = os.getenv("MODEL_NAME", "Phi-3.5-Vision")

    # Max pixels on longest edge sent to the VLM
    VLM_IMAGE_MAX_DIM: int = _int("VLM_IMAGE_MAX_DIM", 448)
    VLM_JPEG_QUALITY: int = _int("VLM_JPEG_QUALITY", 70)

    # VLM inference timeout and retries
    VLM_TIMEOUT: float = _float("VLM_TIMEOUT", 30.0)
    VLM_MAX_RETRIES: int = _int("VLM_MAX_RETRIES", 2)
    VLM_MAX_TOKENS: int = _int("VLM_MAX_TOKENS", 800)

    # ------------------------------------------------------------------ #
    # Performance / Frame capture
    # ------------------------------------------------------------------ #
    # How often (seconds) to run the analysis cycle per stream
    ANALYSIS_INTERVAL: float = _float("ANALYSIS_INTERVAL", 2.0)

    # Frames kept in the circular buffer per stream;
    # only the latest is used for analysis so 5 is sufficient
    FRAME_BUFFER_SIZE: int = _int("FRAME_BUFFER_SIZE", 5)

    # Capture FPS: how many frames per second to decode from the stream.
    # Lower = less CPU.  Defaults to 1/ANALYSIS_INTERVAL.
    CAPTURE_FPS: float = _float("CAPTURE_FPS", 0)  # 0 = auto (1/ANALYSIS_INTERVAL)

    # Resize captured frames to this height (0 = no resize)
    CAPTURE_RESIZE_HEIGHT: int = _int("CAPTURE_RESIZE_HEIGHT", 720)

    # ------------------------------------------------------------------ #
    # Agentic framework (Google ADK)
    # ------------------------------------------------------------------ #
    # Set USE_ADK=true and GEMINI_API_KEY to enable LLM-driven tool calling.
    # When disabled, tools are invoked rule-based (directly from alert config).
    USE_ADK: bool = _bool("USE_ADK", False)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # Model used by the ADK action agent (reasoning, NOT visual)
    ADK_MODEL: str = os.getenv("ADK_MODEL", "gemini-2.0-flash-lite")

    # ------------------------------------------------------------------ #
    # Agentic framework — Local LLM (OpenAI-compatible endpoint)
    # ------------------------------------------------------------------ #
    # Set USE_LOCAL_LLM=true to use a locally hosted text LLM instead of
    # Google ADK.  USE_ADK takes precedence when both are true.
    # Compatible backends: Ollama, LM Studio, vLLM, OVMS text model, etc.
    USE_LOCAL_LLM: bool = _bool("USE_LOCAL_LLM", False)

    # Base URL of the OpenAI-compatible chat/completions endpoint.
    # Ollama default: http://localhost:11434/v1
    # LM Studio default: http://localhost:1234/v1
    LOCAL_LLM_URL: str = os.getenv("LOCAL_LLM_URL", "http://localhost:11434/v1")

    # Model identifier as understood by the local server
    LOCAL_LLM_MODEL: str = os.getenv("LOCAL_LLM_MODEL", "llama3.2")

    # Placeholder API key — most local servers accept any non-empty string
    LOCAL_LLM_API_KEY: str = os.getenv("LOCAL_LLM_API_KEY", "local")

    # Request timeout in seconds for the local LLM (can be slower than cloud)
    LOCAL_LLM_TIMEOUT: float = _float("LOCAL_LLM_TIMEOUT", 30.0)

    # ------------------------------------------------------------------ #
    # Alert behaviour
    # ------------------------------------------------------------------ #
    # Maximum number of alert history entries kept in memory
    ALERT_HISTORY_SIZE: int = _int("ALERT_HISTORY_SIZE", 500)

    # ------------------------------------------------------------------ #
    # Action tools — Email (SMTP)
    # ------------------------------------------------------------------ #
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _int("SMTP_PORT", 587)
    SMTP_USE_TLS: bool = _bool("SMTP_USE_TLS", True)
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    ALERT_EMAIL_FROM: str = os.getenv("ALERT_EMAIL_FROM", "")
    # Comma-separated list of default recipient addresses
    ALERT_EMAIL_TO: str = os.getenv("ALERT_EMAIL_TO", "")

    # ------------------------------------------------------------------ #
    # Action tools — Webhook
    # ------------------------------------------------------------------ #
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    # Optional HMAC-SHA256 secret to sign webhook payloads
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

    # ------------------------------------------------------------------ #
    # Action tools — MQTT
    # ------------------------------------------------------------------ #
    MQTT_BROKER: str = os.getenv("MQTT_BROKER", "")
    MQTT_PORT: int = _int("MQTT_PORT", 1883)
    MQTT_USERNAME: str = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD: str = os.getenv("MQTT_PASSWORD", "")
    # Default base topic; alert name is appended as a sub-topic
    MQTT_BASE_TOPIC: str = os.getenv("MQTT_BASE_TOPIC", "live-video-alerts")

    # ------------------------------------------------------------------ #
    # Action tools — Snapshot
    # ------------------------------------------------------------------ #
    SNAPSHOT_DIR: str = os.getenv("SNAPSHOT_DIR", "snapshots")


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
