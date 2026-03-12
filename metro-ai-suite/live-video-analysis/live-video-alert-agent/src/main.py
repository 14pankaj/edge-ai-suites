# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
Live Video Alert Agent — FastAPI application entry-point.

Endpoints
---------
GET  /                      Dashboard UI (HTML)
GET  /health                Health check (liveness probe)
GET  /ready                 Readiness probe (K8s / Docker healthcheck)
GET  /metrics               System + per-stream metrics (JSON)

GET  /events                SSE stream (real-time analysis + alert_action events)
GET  /video_feed            MJPEG stream for a single stream
GET  /data                  Legacy polling endpoint (fallback)

GET  /streams               List active streams with status
POST /streams               Register a new stream
DELETE /streams/{id}        Remove a stream

GET  /config/alerts         Get alert configurations
POST /config/alerts         Update alert configurations

GET  /tools                 List registered action tools
POST /tools/{name}/invoke   Manually invoke a tool

GET  /alerts/history        Query alert history
"""

import asyncio
import json
import logging
import os
import time
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import cv2
import psutil
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.agent_manager import AgentManager
from src.config import settings, setup_logging
from src.schemas.api import (
    ErrorResponse,
    HealthResponse,
    StreamAddRequest,
    StreamResponse,
    StreamStatus,
    SystemMetrics,
    ToolInfo,
    ToolInvokeRequest,
    ToolInvokeResponse,
)
from src.schemas.monitor import AlertConfig, AlertSeverity

setup_logging()
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Startup / shutdown time tracking
# ------------------------------------------------------------------ #
_startup_time: float = time.monotonic()

# ------------------------------------------------------------------ #
# Authentication
# ------------------------------------------------------------------ #
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: Optional[str] = Security(_api_key_header)):
    """
    If API_KEY is configured, all write and sensitive endpoints require the
    key in the ``X-API-Key`` header.  Read-only UI endpoints are always open.
    """
    if settings.API_KEY and key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


# ------------------------------------------------------------------ #
# Application factory / lifespan
# ------------------------------------------------------------------ #
manager: Optional[AgentManager] = None
_manager_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, _manager_task

    logger.info(
        f"Starting Live Video Alert Agent | VLM={settings.VLM_URL} "
        f"model={settings.MODEL_NAME} ADK={'on' if settings.USE_ADK else 'off'}"
    )

    manager = AgentManager(
        vlm_url=settings.VLM_URL,
        vlm_api_key=settings.VLM_API_KEY,
        model_name=settings.MODEL_NAME,
    )

    if settings.RTSP_URL:
        manager.add_stream("default", settings.RTSP_URL)

    # manager.start() keeps an internal keep-alive loop; wrap in a Task
    # and store a reference so we can handle its exceptions
    _manager_task = asyncio.create_task(manager.start(), name="manager-main")

    def _on_manager_done(t: asyncio.Task):
        if not t.cancelled() and t.exception():
            logger.critical(f"AgentManager crashed: {t.exception()}")

    _manager_task.add_done_callback(_on_manager_done)

    yield

    # Graceful shutdown
    logger.info("Shutting down ...")
    if manager:
        manager.stop()
    if _manager_task and not _manager_task.done():
        _manager_task.cancel()
        try:
            await asyncio.wait_for(_manager_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


app = FastAPI(
    title="Live Video Alert Agent",
    description=(
        "Real-time multi-camera alert detection powered by OpenVINO VLM "
        "and a Google ADK action agent."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "static")
if os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ------------------------------------------------------------------ #
# Helper
# ------------------------------------------------------------------ #

def _require_manager() -> AgentManager:
    if manager is None:
        raise HTTPException(status_code=503, detail="Manager not initialised")
    return manager


# ------------------------------------------------------------------ #
# Health & Readiness
# ------------------------------------------------------------------ #

@app.get("/health", response_model=HealthResponse, tags=["Observability"])
async def health():
    """Liveness probe — always returns 200 if the process is alive."""
    mgr = manager
    return HealthResponse(
        status="healthy",
        streams_active=len(mgr.streams) if mgr else 0,
        agents_enabled=sum(1 for a in mgr.alerts if a.enabled) if mgr else 0,
        vlm_reachable=True,   # TODO: could ping OVMS /v1/config
        uptime_seconds=time.monotonic() - _startup_time,
        timestamp=datetime.now(tz=timezone.utc),
    )


@app.get("/ready", tags=["Observability"])
async def ready():
    """
    Readiness probe — returns 200 only when the manager is running and
    at least one alert is enabled.
    """
    if manager is None:
        raise HTTPException(status_code=503, detail="Manager not ready")
    enabled = sum(1 for a in manager.alerts if a.enabled)
    if enabled == 0:
        raise HTTPException(status_code=503, detail="No alerts enabled")
    return {"status": "ready", "streams": len(manager.streams), "alerts": enabled}


# ------------------------------------------------------------------ #
# Metrics
# ------------------------------------------------------------------ #

@app.get("/metrics", response_model=SystemMetrics, tags=["Observability"])
async def metrics():
    """System CPU/memory and per-stream inference counters."""
    mgr = _require_manager()
    stream_metrics = mgr.get_stream_metrics()
    return SystemMetrics(
        cpu_percent=psutil.cpu_percent(interval=None),
        memory_percent=psutil.virtual_memory().percent,
        streams=[
            {
                "stream_id": m["stream_id"],
                "analysis_count": m["analysis_count"],
                "alert_count": m["alert_count"],
                "last_inference_ms": m["last_inference_ms"],
            }
            for m in stream_metrics
        ],
    )


# ------------------------------------------------------------------ #
# SSE events
# ------------------------------------------------------------------ #

async def _event_generator(request: Request):
    """
    SSE generator yielding:
    - ``init``         — on connection (current streams + latest results)
    - ``analysis``     — per-stream VLM results
    - ``alert_action`` — enriched event when tools are invoked
    - ``keepalive``    — every 15 s to prevent proxy timeouts
    """
    mgr = manager
    if not mgr:
        yield {"event": "error", "data": json.dumps({"message": "Manager not initialised"})}
        return

    queue = await mgr.subscribe()
    try:
        yield {
            "event": "init",
            "data": json.dumps({
                "results": mgr.latest_results,
                "streams": list(mgr.streams.keys()),
            }),
        }

        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield {"event": event["event"], "data": json.dumps(event["data"])}
            except asyncio.TimeoutError:
                yield {"event": "keepalive", "data": json.dumps({"ts": time.monotonic()})}

    except (asyncio.CancelledError, GeneratorExit):
        pass
    except Exception as exc:
        logger.error(f"SSE error: {exc}")
        yield {"event": "error", "data": json.dumps({"message": str(exc)})}
    finally:
        await mgr.unsubscribe(queue)


@app.get("/events", tags=["Streaming"])
async def sse_events(request: Request):
    """Server-Sent Events stream for real-time analysis and alert actions."""
    return EventSourceResponse(
        _event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ #
# Video feed (MJPEG)
# ------------------------------------------------------------------ #

async def _mjpeg_generator(stream_id: str):
    while True:
        if manager is None:
            break
        frame = manager.get_latest_frame(stream_id)
        if frame is not None:
            ret, buf = cv2.imencode(
                ".jpg", frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80],
            )
            if ret:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + buf.tobytes()
                    + b"\r\n"
                )
            await asyncio.sleep(0.033)   # ~30 fps display cap
        else:
            await asyncio.sleep(0.1)


@app.get("/video_feed", tags=["Streaming"])
async def video_feed(stream_id: str = "default"):
    """MJPEG stream for the dashboard video tiles."""
    return StreamingResponse(
        _mjpeg_generator(stream_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ------------------------------------------------------------------ #
# Legacy polling
# ------------------------------------------------------------------ #

@app.get("/data", tags=["Streaming"])
async def get_data():
    """Legacy polling endpoint — prefer /events (SSE) for new integrations."""
    if manager:
        return JSONResponse(content=manager.latest_results)
    return JSONResponse(content={})


# ------------------------------------------------------------------ #
# Stream management
# ------------------------------------------------------------------ #

@app.get("/streams", tags=["Streams"])
async def get_streams():
    """List all active streams with health status."""
    mgr = _require_manager()
    result = []
    for sid, stream_mgr in mgr.streams.items():
        health = stream_mgr.get_health()
        result.append(
            StreamStatus(
                id=sid,
                url=stream_mgr.rtsp_url,
                connected=health.connected,
                fps=round(health.actual_capture_fps, 2),
                resolution=health.resolution,
                buffer_fill=health.buffer_fill,
            ).model_dump()
        )
    return JSONResponse(content={"streams": result})


@app.post("/streams", response_model=StreamResponse, tags=["Streams"])
async def add_stream(
    data: StreamAddRequest = Body(...),
    _: None = Depends(verify_api_key),
):
    """Register a new video stream."""
    mgr = _require_manager()
    if data.id in mgr.streams:
        raise HTTPException(status_code=409, detail=f"Stream '{data.id}' already exists")
    mgr.add_stream(data.id, data.url)
    return StreamResponse(status="added", id=data.id)


@app.delete("/streams/{stream_id}", response_model=StreamResponse, tags=["Streams"])
async def remove_stream(
    stream_id: str,
    _: None = Depends(verify_api_key),
):
    """Remove a registered stream."""
    mgr = _require_manager()
    if stream_id not in mgr.streams:
        raise HTTPException(status_code=404, detail=f"Stream '{stream_id}' not found")
    mgr.remove_stream(stream_id)
    return StreamResponse(status="removed", id=stream_id)


# ------------------------------------------------------------------ #
# Alert configuration
# ------------------------------------------------------------------ #

@app.get("/config/alerts", tags=["Configuration"])
async def get_alerts_config():
    """Return the current alert configurations."""
    mgr = _require_manager()
    return JSONResponse(content=mgr.get_alerts_config())


@app.post("/config/alerts", tags=["Configuration"])
async def update_alerts_config(
    data: List[dict] = Body(...),
    _: None = Depends(verify_api_key),
):
    """
    Replace the full alert configuration.

    Each entry must conform to the AlertConfig schema:
    - name (str, required)
    - prompt (str, required)
    - enabled (bool, default true)
    - severity (low|medium|high|critical, default medium)
    - cooldown_seconds (float >= 0, default 60)
    - tools (list of tool names, default [\"log_alert\"])
    - escalation (optional: {threshold_consecutive, additional_tools})
    """
    mgr = _require_manager()
    try:
        mgr.save_alerts_config(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return JSONResponse(content={"status": "saved", "count": len(data)})


# ------------------------------------------------------------------ #
# Tools
# ------------------------------------------------------------------ #

_TOOL_DESCRIPTIONS = {
    "log_alert": "Log alert event to application log and history (always available)",
    "send_email": "Send SMTP email notification (requires SMTP_HOST)",
    "trigger_webhook": "HTTP POST to an external webhook URL (requires WEBHOOK_URL)",
    "capture_snapshot": "Save current frame as JPEG snapshot (requires SNAPSHOT_DIR writable)",
    "publish_mqtt": "Publish alert to MQTT broker (requires MQTT_BROKER)",
}


@app.get("/tools", tags=["Tools"])
async def list_tools():
    """List all registered action tools and their configuration status."""
    tools = []
    for name, desc in _TOOL_DESCRIPTIONS.items():
        enabled = True
        if name == "send_email" and not settings.SMTP_HOST:
            enabled = False
        elif name == "trigger_webhook" and not settings.WEBHOOK_URL:
            enabled = False
        elif name == "publish_mqtt" and not settings.MQTT_BROKER:
            enabled = False

        tools.append(ToolInfo(name=name, description=desc, enabled=enabled).model_dump())
    return JSONResponse(content={"tools": tools})


@app.post("/tools/{tool_name}/invoke", tags=["Tools"])
async def invoke_tool(
    tool_name: str,
    request: ToolInvokeRequest = Body(default=None),
    _: None = Depends(verify_api_key),
):
    """Manually invoke a registered tool for testing."""
    from src.agentic.tools import (
        log_alert, send_email_alert, trigger_webhook, capture_snapshot, publish_mqtt,
    )

    _TOOL_MAP = {
        "log_alert": log_alert,
        "send_email": send_email_alert,
        "trigger_webhook": trigger_webhook,
        "capture_snapshot": capture_snapshot,
        "publish_mqtt": publish_mqtt,
    }

    fn = _TOOL_MAP.get(tool_name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    params = request.parameters if request else {}
    t0 = time.monotonic()
    try:
        result = await fn(**params)
        duration_ms = (time.monotonic() - t0) * 1000
        return ToolInvokeResponse(
            tool=tool_name,
            status="success",
            result=result,
            duration_ms=round(duration_ms, 1),
        )
    except Exception as exc:
        duration_ms = (time.monotonic() - t0) * 1000
        return ToolInvokeResponse(
            tool=tool_name,
            status="error",
            result={"error": str(exc)},
            duration_ms=round(duration_ms, 1),
        )


# ------------------------------------------------------------------ #
# Alert history
# ------------------------------------------------------------------ #

@app.get("/alerts/history", tags=["Alerts"])
async def get_alert_history(
    stream_id: Optional[str] = None,
    alert_name: Optional[str] = None,
    severity: Optional[str] = None,
    answer: Optional[str] = None,
    limit: int = 50,
):
    """
    Query alert detection history.

    Supports filtering by stream_id, alert_name, severity, and answer.
    Results are returned newest-first.
    """
    mgr = _require_manager()
    sev_enum = None
    if severity:
        try:
            sev_enum = AlertSeverity(severity)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown severity: {severity}")

    events = mgr.alert_state.get_history(
        stream_id=stream_id,
        alert_name=alert_name,
        severity=sev_enum,
        answer=answer,
        limit=min(limit, 500),
    )
    return JSONResponse(content={
        "count": len(events),
        "events": [e.model_dump(mode="json") for e in events],
    })


# ------------------------------------------------------------------ #
# Dashboard UI
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def read_root():
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")
    if not os.path.exists(ui_path):
        return HTMLResponse(content="<h1>UI not found</h1>", status_code=404)
    with open(ui_path) as fh:
        return HTMLResponse(content=fh.read())


# ------------------------------------------------------------------ #
# Entry-point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

