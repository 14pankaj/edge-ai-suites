# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
API request and response schemas (Pydantic models for all FastAPI endpoints).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ------------------------------------------------------------------ #
# Stream management
# ------------------------------------------------------------------ #

class StreamAddRequest(BaseModel):
    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Unique stream identifier (alphanumeric, hyphens, underscores)",
    )
    url: str = Field(..., description="RTSP, HTTP, HTTPS, or file:// URL")

    @field_validator("id")
    @classmethod
    def id_safe(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError("Stream ID may only contain letters, digits, hyphens and underscores")
        return v

    @field_validator("url")
    @classmethod
    def url_scheme(cls, v: str) -> str:
        allowed = ("rtsp://", "rtsps://", "http://", "https://", "file://")
        if not any(v.startswith(s) for s in allowed):
            raise ValueError(f"URL must start with one of: {', '.join(allowed)}")
        return v


class StreamResponse(BaseModel):
    status: Literal["added", "removed"]
    id: str


class StreamStatus(BaseModel):
    id: str
    url: str
    connected: bool
    fps: Optional[float] = None
    resolution: Optional[str] = None
    buffer_fill: int = 0


# ------------------------------------------------------------------ #
# Health & readiness
# ------------------------------------------------------------------ #

class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    streams_active: int
    agents_enabled: int
    vlm_reachable: bool
    uptime_seconds: float
    timestamp: datetime


# ------------------------------------------------------------------ #
# Metrics (Prometheus-ready summary)
# ------------------------------------------------------------------ #

class StreamMetrics(BaseModel):
    stream_id: str
    analysis_count: int = 0
    alert_count: int = 0
    last_inference_ms: Optional[float] = None


class SystemMetrics(BaseModel):
    cpu_percent: float
    memory_percent: float
    streams: List[StreamMetrics] = Field(default_factory=list)


# ------------------------------------------------------------------ #
# Tools
# ------------------------------------------------------------------ #

class ToolInfo(BaseModel):
    name: str
    description: str
    enabled: bool
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ToolInvokeRequest(BaseModel):
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResponse(BaseModel):
    tool: str
    status: Literal["success", "error"]
    result: Any
    duration_ms: float


# ------------------------------------------------------------------ #
# Alert history
# ------------------------------------------------------------------ #

class AlertHistoryQuery(BaseModel):
    stream_id: Optional[str] = None
    alert_name: Optional[str] = None
    severity: Optional[str] = None
    answer: Optional[Literal["YES", "NO"]] = None
    limit: int = Field(default=50, ge=1, le=500)


# ------------------------------------------------------------------ #
# Standard error envelope
# ------------------------------------------------------------------ #

class ErrorResponse(BaseModel):
    error: str
    detail: str
    code: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
