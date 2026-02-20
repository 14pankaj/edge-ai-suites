# Live Video Alert Agent — Code Review & Recommendations

**Reviewed:** February 20, 2026
**Scope:** Full codebase review of `metro-ai-suite/live-video-analysis/live-video-alert-agent`
**Version:** 1.0.0-rc.0

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Code Review — Logic & Syntax Correctness](#2-code-review--logic--syntax-correctness)
3. [Recommendations — Performance](#3-recommendations--performance)
4. [Recommendations — Agentic Framework](#4-recommendations--agentic-framework)
5. [Recommendations — API & Schema Changes](#5-recommendations--api--schema-changes)
6. [Recommendations — Multi-Camera & Scalability](#6-recommendations--multi-camera--scalability)
7. [Recommendations — Documentation](#7-recommendations--documentation)
8. [Recommendations — Testing & CI/CD](#8-recommendations--testing--cicd)
9. [Recommendations — Security & Deployment](#9-recommendations--security--deployment)
10. [Recommendations — UI/UX](#10-recommendations--uiux)
11. [Priority Matrix](#11-priority-matrix)

---

## 1. Executive Summary

The Live Video Alert Agent is a functional prototype that ingests RTSP streams, runs VLM inference via OVMS, and serves real-time alert results over SSE to a web dashboard. The core data flow works end-to-end, but there are significant gaps across six dimensions:

| Dimension | Current State | Gap Severity |
|---|---|---|
| **Logic/Syntax** | Functional with minor defects | Medium |
| **Performance** | Sequential, single-threaded analysis; no frame throttling | High |
| **Agentic Framework** | No tool-calling, no ADK, no action pipeline | Critical |
| **API Design** | Raw dicts, no validation, no auth, missing endpoints | High |
| **Multi-Camera** | Serial processing, UI limits 3 streams | High |
| **Documentation** | Partial; architecture, developer, and tuning guides missing | Medium |

---

## 2. Code Review — Logic & Syntax Correctness

### 2.1 `src/main.py`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 1 | **Fire-and-forget task without error handling.** `asyncio.create_task(manager.start())` — if the analysis loop raises an unhandled exception, it silently dies. The task reference is not stored, so the exception is never retrieved. | High | L43 |
| 2 | **No graceful shutdown.** `manager.stop()` sets `self.running = False` but does not `await` the analysis coroutine to finish. In-flight VLM requests may be dropped mid-call. | Medium | L47 |
| 3 | **Global mutable state.** `manager` is a module-level global accessed by all request handlers without any concurrency guard. While FastAPI is single-threaded per event loop, this pattern is fragile and breaks if workers > 1. | Low | L24 |
| 4 | **MJPEG generator never terminates.** `generate_frames()` runs an infinite loop. If the manager is `None`, it breaks, but if the client disconnects, the generator keeps running until the next iteration detects the manager state. There is no `request.is_disconnected()` check. | Medium | L60–72 |

### 2.2 `src/core/agent_manager.py`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 5 | **Deprecated `asyncio.get_event_loop().time()`.** Should use `asyncio.get_running_loop().time()` or `time.monotonic()`. | Low | L113, L161 |
| 6 | **Relative file path for config.** `config_file="resources/streams.json"` is relative to CWD, not the module. If the app is launched from a different directory, config loading will silently fail. | Medium | L17 |
| 7 | **No structural validation of agents config loaded from JSON.** The loaded JSON is trusted blindly — missing keys (`name`, `prompt`, `enabled`) will cause `KeyError` at runtime in `_build_dynamic_prompt()`. | Medium | L44–52 |
| 8 | **String-based prompt assembly is fragile.** `_build_dynamic_prompt()` uses f-strings with `', '.join(...)` to build a JSON template inside a prompt string. If agent names contain quotes or special characters, the prompt becomes invalid. | Medium | L103–121 |
| 9 | **Sequential stream analysis.** The `for stream_id in stream_ids` loop in `_run_analysis_loop()` processes each stream sequentially. Each VLM call blocks the loop, so with N streams, the effective analysis interval is `N × VLM_latency + ANALYSIS_INTERVAL`. | High | L128–162 |
| 10 | **Agents config saved to disk on every API call but not reloaded in the loop.** The analysis loop reads `self.agents_config` which is updated in-memory by `save_agents_config()`. This works in a single-process model but the in-memory and on-disk states can diverge on crash. | Low | L56–65 |

### 2.3 `src/core/stream_manager.py`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 11 | **Full-FPS frame ingestion with no throttling.** `_ingest_loop()` calls `cap.read()` in a tight loop, decoding every frame at the camera's native FPS (25-30fps). Only ~1 frame/sec is consumed by the analysis loop. This wastes 96-97% of CPU cycles on decoding unused frames. | High | L36–56 |
| 12 | **No stream health reporting.** There is no way to query whether a stream is connected, disconnected, or in a reconnection state. The stream list endpoint only returns IDs. | Medium | — |
| 13 | **Fixed reconnection delays.** `time.sleep(5)` and `time.sleep(2)` are hardcoded. There is no exponential backoff, no configurable timeout, and no max-retry limit. A permanently unavailable stream will retry forever. | Low | L44, L50 |
| 14 | **Full-resolution frames stored in buffer.** The deque stores raw OpenCV frames at capture resolution (e.g., 1920×1080). With `FRAME_BUFFER_SIZE=30` and multiple streams, this consumes significant memory (a single 1080p frame ≈ 6MB, so 30 frames ≈ 180MB per stream). | Medium | L12, L36 |

### 2.4 `src/core/vlm_client.py`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 15 | **`max_retries=0` — no retry on transient failures.** A single network hiccup or OVMS timeout will return `None`, causing that analysis cycle to produce no results. | Medium | L14 |
| 16 | **System prompt placed in user content.** The `system_prompt` is concatenated into the user message content instead of being sent as a separate `{"role": "system", ...}` message. This may degrade instruction-following quality on some VLMs. | Medium | L37 |
| 17 | **Hardcoded image parameters.** Max dimension (448px), JPEG quality (70) are hardcoded. These should be configurable, especially since different VLMs have different optimal input resolutions (e.g., InternVL2 supports 448, Phi-3.5-Vision supports higher). | Low | L23, L28 |
| 18 | **No streaming/chunked response handling.** The VLM client waits for the full response. For larger models or longer reasoning chains, streaming (`stream=True`) would reduce time-to-first-token. | Low | L42 |

### 2.5 `src/schemas/monitor.py`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 19 | **Only one schema defined.** All API endpoints use raw `dict`/`list` types. No Pydantic models for stream add/remove requests, agent config, SSE events, or API responses. | Medium | Full file |

### 2.6 `docker-compose.yml`

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 20 | **`network_mode: host` on both services.** This bypasses Docker networking, exposing all ports directly. The `ports:` mappings are ignored under host networking. Not portable across OS or orchestration platforms. | Medium | L43, L73 |
| 21 | **Container name mismatch with docs.** docker-compose defines `container_name: live-video-alert` but the get-started.md references `docker logs agentic-nvr`. | Low | L62 vs docs |
| 22 | **No GPU passthrough configuration.** Despite claiming Intel GPU support, there are no `device` mappings or `--device /dev/dri` flags for GPU acceleration in OVMS. | Medium | — |

### 2.7 Frontend (`src/ui/static/js/app.js`)

| # | Issue | Severity | Location |
|---|-------|----------|----------|
| 23 | **Hardcoded 3-stream limit in UI.** `addNewStream()` checks `activeStreams.length >= 3` — this limit is not documented, not configurable, and differs from the backend which has no stream limit. | Medium | L401 |
| 24 | **No SSE reconnection with backoff.** `EventSource` `onerror` falls back to polling but never retries SSE. The browser's built-in reconnection is relied upon, but when `readyState === CLOSED`, it's permanently abandoned. | Medium | L105–113 |
| 25 | **CDN dependency.** `<script src="https://cdn.tailwindcss.com">` requires internet access. Will fail in air-gapped/edge deployments. | High | index.html L9 |

---

## 3. Recommendations — Performance

### 3.1 Frame Ingestion Optimization (Critical)

**Current Problem:** The `LiveStreamManager._ingest_loop()` decodes every frame at native camera FPS (25-30fps). Only ~1 fps is analyzed.

**Recommendation:**
- Use OpenCV `grab()` to skip frames without decoding, and only `retrieve()` at the target analysis FPS.
- Add a configurable `capture_fps` parameter so the ingestion rate matches the analysis rate.

```python
# Proposed pattern
def _ingest_loop(self):
    cap = cv2.VideoCapture(self.rtsp_url)
    target_interval = 1.0 / self.capture_fps  # e.g., 1.0 for 1fps
    
    while self.running:
        grabbed = cap.grab()  # Skip decode — fast
        if not grabbed:
            # reconnect logic ...
            continue
        
        now = time.monotonic()
        if now - self._last_capture >= target_interval:
            ret, frame = cap.retrieve()  # Decode only when needed
            if ret:
                # Optionally resize at capture time to reduce memory
                frame = cv2.resize(frame, (self.capture_width, self.capture_height))
                with self._lock:
                    self.frame_buffer.append(frame)
                self._last_capture = now
```

**Impact:** ~95% reduction in CPU usage for frame decoding per stream.

### 3.2 Concurrent Stream Analysis (Critical)

**Current Problem:** Streams are analyzed sequentially in `_run_analysis_loop()`. With N streams, latency = `N × VLM_inference_time`.

**Recommendation:**
- Use `asyncio.gather()` to process all streams concurrently within each analysis cycle.
- Alternatively, launch a dedicated analysis coroutine per stream.

```python
# Option A: Concurrent gather
async def _run_analysis_loop(self):
    while self.running:
        prompt = self._build_dynamic_prompt()
        if not prompt:
            await asyncio.sleep(1)
            continue
        
        tasks = [
            self._analyze_stream(stream_id, prompt)
            for stream_id in list(self.streams.keys())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(settings.ANALYSIS_INTERVAL)
```

**Impact:** Analysis latency becomes `max(VLM_latency)` instead of `sum(VLM_latency)` across streams.

### 3.3 Frame Buffer Memory Optimization (Medium)

**Current Problem:** Full-resolution frames stored in deque (30 frames × ~6MB each at 1080p = ~180MB per stream).

**Recommendation:**
- Resize frames at capture time to the VLM's required resolution (e.g., 448×448 or 720p).
- Reduce `FRAME_BUFFER_SIZE` to 3-5 since only the latest frame is used.
- Store pre-encoded JPEG bytes in the buffer for streams that also serve MJPEG.

### 3.4 VLM Request Optimization (Medium)

**Recommendations:**
- Add retry logic with exponential backoff (1-2 retries max for latency-sensitive alerting).
- Use `system` role for system prompts instead of concatenating into user content.
- Make image resolution and JPEG quality configurable per model via `config.py`.
- Consider caching the base64-encoded frame if the same frame is sent to multiple VLM prompts.

### 3.5 MJPEG Stream Optimization (Low)

**Current Problem:** `generate_frames()` encodes every frame to JPEG and loops with only 10ms sleep — effectively re-encoding and serving frames at ~100fps even though the source may be 1fps.

**Recommendation:**
- Match the MJPEG output rate to the capture rate.
- Cache the last encoded JPEG and only re-encode when a new frame arrives.

---

## 4. Recommendations — Agentic Framework

### 4.1 Google ADK Integration (Critical)

**Current State:** The application has zero agentic framework integration. "Agents" are currently just prompt strings — there is no tool calling, planning, memory, or reasoning chain.

**Recommendation:** Integrate [Google Agent Development Kit (ADK)](https://github.com/google/adk-python) to transform the alert detection pipeline into a true agentic system.

**Proposed Architecture:**
```
Camera Stream → Frame Capture → VLM Analysis (alert detection)
                                       ↓
                               ADK Agent Orchestrator
                                       ↓
                              ┌────────┼────────┐
                              ↓        ↓        ↓
                         Tool: Email  Tool:    Tool: 
                         Notify      Webhook  Log/DB
                              ↓        ↓        ↓
                           Action    Action   Action
                           Result    Result   Result
                              ↓        ↓        ↓
                              └────────┼────────┘
                                       ↓
                                  Event Manager → SSE → Dashboard
```

**Implementation Outline:**
1. **Define an ADK Agent** for alert processing that receives VLM analysis results.
2. **Register tools** that the agent can invoke:
   - `send_email_alert(recipient, subject, body)` — Send email notification
   - `trigger_webhook(url, payload)` — Call an external webhook/API
   - `log_alert_to_database(alert_data)` — Persist alert to a database
   - `capture_snapshot(stream_id)` — Save a high-resolution snapshot
   - `trigger_alarm(zone_id)` — Activate a physical alarm system
   - `send_mqtt_message(topic, payload)` — Publish to an MQTT broker
3. **Agent reasoning:** The ADK agent should decide which tools to invoke based on alert severity, frequency, and configured rules.
4. **Memory/State:** Use ADK's session/state management to track alert history, prevent duplicate notifications (deduplication), and maintain context across cycles.

```python
# Proposed ADK integration (conceptual)
from google.adk import Agent, Tool

# Define tools
@Tool
def send_email_alert(recipient: str, subject: str, body: str) -> str:
    """Send an email notification for a detected alert."""
    # Implementation
    return "Email sent successfully"

@Tool
def trigger_webhook(url: str, payload: dict) -> str:
    """Call an external webhook with alert data."""
    # Implementation
    return "Webhook triggered"

@Tool
def capture_snapshot(stream_id: str) -> str:
    """Capture and save a high-resolution snapshot from the stream."""
    # Implementation
    return "Snapshot saved to /snapshots/..."

# Define the alert agent
alert_agent = Agent(
    name="AlertActionAgent",
    model="gemini-2.0-flash",  # or local model
    instruction="""You are an alert action agent. When you receive a video 
    analysis result with YES alerts, determine the appropriate actions:
    - For fire/smoke: trigger_webhook + send_email_alert + capture_snapshot
    - For intrusion: capture_snapshot + send_email_alert
    - For safety violations: log and notify
    Use your judgment for severity and choose tools accordingly.""",
    tools=[send_email_alert, trigger_webhook, capture_snapshot]
)
```

### 4.2 Tool Registry & Plugin System (High)

**Recommendation:** Create a pluggable tool registry so users can add custom actions without modifying core code.

```python
# Proposed tool registry
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Callable] = {}
    
    def register(self, name: str, handler: Callable, description: str):
        self._tools[name] = {"handler": handler, "description": description}
    
    def get_tool(self, name: str) -> Callable:
        return self._tools[name]["handler"]
    
    def list_tools(self) -> List[Dict]:
        return [{"name": k, "description": v["description"]} for k, v in self._tools.items()]
```

### 4.3 Alert Deduplication & Suppression (High)

**Current Problem:** Every analysis cycle that detects a "YES" broadcasts an alert. If fire is detected continuously for 5 minutes at 1fps, that's 300 identical alerts.

**Recommendation:**
- Implement alert state tracking with cooldown periods.
- Only broadcast on state transitions (NO → YES, YES → NO).
- Add configurable suppression windows per alert type.

### 4.4 Alert Severity & Escalation (Medium)

**Recommendation:**
- Extend the `AgentResult` schema to include a `severity` field (low/medium/high/critical).
- Implement escalation rules: if an alert persists beyond a threshold, escalate to a higher-priority action.
- The ADK agent can use history/memory to reason about escalation.

### 4.5 Multi-Agent Coordination (Medium)

**Recommendation:**
- Allow agents to share context. For example, a "Person Detection" agent detecting a person could provide bounding-box context to a "Safety Vest Detection" agent.
- ADK supports multi-agent architectures with delegation — use a root agent to coordinate sub-agents per alert type.

---

## 5. Recommendations — API & Schema Changes

### 5.1 Pydantic Request/Response Models (High)

**Current Problem:** All endpoints use raw `dict`/`list` — no validation, no auto-generated OpenAPI docs.

**Recommendation:** Define proper Pydantic models for every endpoint.

```python
# Proposed schemas
from pydantic import BaseModel, Field, HttpUrl
from typing import List, Optional, Literal
from datetime import datetime

class StreamAddRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    url: str = Field(..., description="RTSP or file URL")

class StreamResponse(BaseModel):
    status: Literal["added", "removed"]
    id: str

class AgentConfig(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    prompt: str = Field(..., min_length=5, max_length=500)
    enabled: bool = True

class AgentResult(BaseModel):
    answer: Literal["YES", "NO"]
    reason: str
    confidence: Optional[float] = None
    timestamp: Optional[datetime] = None

class AnalysisEvent(BaseModel):
    stream_id: str
    results: Dict[str, AgentResult]
    timestamp: datetime

class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    streams: int
    agents: int
    vlm_connected: bool
    uptime_seconds: float

class StreamStatus(BaseModel):
    id: str
    url: str
    connected: bool
    fps: Optional[float]
    resolution: Optional[str]
    buffer_fill: int
```

### 5.2 Missing API Endpoints (High)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check for container orchestration (K8s readiness/liveness probes) |
| `/ready` | GET | Readiness probe — returns 200 only when VLM is connected and at least one stream is active |
| `/metrics` | GET | Prometheus-compatible metrics (inference latency, FPS, alert counts) |
| `/streams/{id}/status` | GET | Individual stream health (connected, FPS, resolution, buffer fill) |
| `/alerts/history` | GET | Query historical alerts with pagination and filtering |
| `/config` | GET/PUT | Full application configuration (not just agents) |
| `/tools` | GET | List registered action tools (for agentic framework) |
| `/tools/{name}/invoke` | POST | Manually invoke a tool for testing |

### 5.3 OpenAPI & Versioning (Medium)

**Recommendations:**
- Add API versioning: prefix all endpoints with `/api/v1/`.
- Customize FastAPI's OpenAPI metadata (title, description, version, contact).
- Add response models to all endpoint decorators for auto-generated documentation.
- Add `tags` to group endpoints logically in the OpenAPI docs.

### 5.4 Input Validation (Medium)

**Recommendations:**
- Validate RTSP URLs against allowed schemes (`rtsp://`, `http://`, `https://`, `file://`).
- Validate stream IDs against a safe character pattern (alphanumeric + hyphen + underscore).
- Enforce max prompt length to prevent VLM token limit issues.
- Validate agent names for uniqueness at the schema level.

### 5.5 Error Response Standardization (Low)

**Recommendation:** Define a consistent error response format:

```python
class ErrorResponse(BaseModel):
    error: str
    detail: str
    code: str  # Machine-readable error code
    timestamp: datetime
```

---

## 6. Recommendations — Multi-Camera & Scalability

### 6.1 Concurrent Multi-Stream Processing (Critical)

**Current Problem:** The analysis loop processes streams sequentially. With 4 cameras and 2-second VLM inference, the cycle time is 8+ seconds per round.

**Recommendations:**
- Process streams concurrently using `asyncio.gather()` or dedicated per-stream analysis tasks.
- Consider a worker pool pattern with configurable concurrency limits.
- Allow independent analysis intervals per stream (some cameras may need faster cadence than others).

### 6.2 Remove Arbitrary Stream Limits (High)

**Current Problem:** UI limits to 3 streams, API limits agents to 4 — both are arbitrary and hardcoded.

**Recommendations:**
- Make stream and agent limits configurable via environment variables.
- UI grid should dynamically adapt — currently uses `grid-cols-1/2/3/4` which already supports up to 4 columns.
- Backend should enforce limits based on available resources, not hardcoded values.

### 6.3 Per-Stream Configuration (Medium)

**Recommendation:** Allow per-stream overrides for:
- Analysis interval (some cameras need faster/slower analysis).
- Agent selection (not all agents need to run on all cameras).
- Frame resolution and quality settings.
- Alert routing (different cameras → different notification channels).

### 6.4 Stream Grouping & Zones (Low)

**Recommendation:** Support stream grouping into logical zones (e.g., "Building Entrance", "Parking Lot") for easier management and zone-level alert aggregation.

---

## 7. Recommendations — Documentation

### 7.1 Missing Documentation (High)

| Document | Current State | Recommendation |
|----------|--------------|----------------|
| **Architecture Guide** | `overview.md` has a basic text diagram | Add detailed component diagrams (Mermaid/PlantUML), data flow descriptions, and technology choices rationale |
| **Developer Guide** | Missing entirely | Add setup instructions for local development, code structure walkthrough, and contribution guidelines |
| **Configuration Reference** | Scattered across `get-started.md` | Create a dedicated `configuration.md` with all env vars, defaults, allowed values, and impact description |
| **Performance Tuning Guide** | Missing entirely | Add guidance on tuning `ANALYSIS_INTERVAL`, `FRAME_BUFFER_SIZE`, model selection, and hardware utilization |
| **Security Guide** | Missing entirely | Document authentication approach, input sanitization, network security, and container hardening |
| **Multi-Camera Deployment** | Missing entirely | Add a guide for deploying with multiple cameras including performance expectations |
| **Agentic Framework Guide** | Missing entirely — critical for the application's stated objective | Document the tool-calling architecture, how to add custom tools, and the ADK integration |
| **Troubleshooting / FAQ** | `known-issues.md` exists but limited | Expand with common deployment scenarios, performance diagnostics, and log interpretation |

### 7.2 Inline Code Documentation (Medium)

**Current Problem:** Several modules lack docstrings or have incomplete ones.

**Recommendations:**
- Add module-level docstrings to all Python files explaining purpose and usage.
- Add class-level docstrings with usage examples.
- Add parameter-level documentation to all public methods.
- `EventManager` is well-documented — use it as the standard for other modules.

### 7.3 Container Name & Reference Inconsistencies (Low)

**Current Problem:** docker-compose uses `container_name: live-video-alert` but `get-started.md` references `docker logs agentic-nvr`. The application is called "Live Video Alert Agent" in the README but "Agentic Alert NVR" in the UI title.

**Recommendation:** Standardize naming across all documentation, docker-compose, and UI elements.

### 7.4 Diagram & Visual Assets (Low)

**Recommendation:** Add:
- Architecture diagram (SVG/PNG) for the README and overview.
- Screenshot of the dashboard UI.
- Sequence diagram for the alert detection flow.
- Deployment topology diagram for multi-camera setups.

---

## 8. Recommendations — Testing & CI/CD

### 8.1 Unit Tests (High)

**Current State:** Zero test files.

**Recommendations:**
- Add `tests/` directory with `pytest` as the test framework.
- Unit tests for:
  - `VLMClient._encode_image()` — various resolutions, edge cases.
  - `AgentManager._build_dynamic_prompt()` — various agent configs, edge cases.
  - `EventManager` — subscribe/unsubscribe/broadcast lifecycle.
  - `AgentResult` schema validation.
  - JSON response parsing and error handling in `_run_analysis_loop()`.
  - Config loading with missing/corrupt files.

### 8.2 Integration Tests (Medium)

**Recommendations:**
- API endpoint tests using FastAPI's `TestClient`.
- Mock VLM responses to test the full analysis pipeline without a live OVMS instance.
- Stream management lifecycle tests (add, status, remove).

### 8.3 CI/CD Pipeline (Medium)

**Recommendations:**
- Add GitHub Actions / GitLab CI workflow for:
  - Linting (`ruff` or `flake8`).
  - Type checking (`mypy`).
  - Unit tests (`pytest`).
  - Docker build validation.
  - Security scanning (`bandit`, container image scanning).

---

## 9. Recommendations — Security & Deployment

### 9.1 Authentication & Authorization (High)

**Current Problem:** The API is completely open — anyone with network access can add/remove streams, modify agents, and view video feeds.

**Recommendations:**
- Add API key authentication as a minimum (via header or query parameter).
- Consider OAuth2/JWT for production deployments.
- Add role-based access: viewer (read-only dashboard), operator (manage streams/agents), admin (full access).

### 9.2 CORS Configuration (Medium)

**Current Problem:** No CORS middleware configured. If the dashboard is served from a different origin (e.g., behind a reverse proxy), API calls will fail.

**Recommendation:** Add FastAPI CORS middleware with configurable allowed origins.

### 9.3 Input Sanitization (Medium)

**Current Problem:** Stream IDs and agent prompts are used directly in file paths (`resources/streams.json`), prompt strings, and HTML rendering.

**Recommendations:**
- Validate and sanitize all user inputs (stream IDs, URLs, agent names, prompts).
- Use Pydantic validators with regex patterns for IDs.
- Ensure prompts don't contain injection payloads for the VLM.

### 9.4 Container Hardening (Medium)

**Recommendations:**
- Replace `network_mode: host` with explicit port mappings and a Docker bridge network.
- Add GPU device passthrough (`/dev/dri`) for Intel GPU acceleration in OVMS.
- Add resource limits (`mem_limit`, `cpus`) to containers.
- Use a read-only root filesystem for the application container.
- Add health checks to the `live-video-alert` container.

### 9.5 Air-Gapped Deployment Support (Medium)

**Current Problem:** The UI loads TailwindCSS from a CDN (`cdn.tailwindcss.com`). This fails in air-gapped edge deployments.

**Recommendation:**
- Bundle TailwindCSS as a pre-compiled CSS file in `/static/css/`.
- Bundle the Inter font as a local asset or use system fonts.

### 9.6 Environment Validation (Low)

**Recommendation:** Add startup validation in `config.py` to verify:
- `VLM_URL` is reachable (optional connectivity check).
- `PORT` is a valid port number.
- `LOG_LEVEL` is a valid Python logging level.
- `ANALYSIS_INTERVAL` is positive.
- `FRAME_BUFFER_SIZE` is within reasonable bounds.

---

## 10. Recommendations — UI/UX

### 10.1 System Metrics Implementation (Medium)

**Current Problem:** The dashboard shows CPU and Memory bars but they are placeholder — always showing 0%.

**Recommendation:**
- Add a `/metrics` API endpoint that returns system metrics (CPU, memory, GPU utilization).
- Use a WebSocket or SSE channel to push metrics to the dashboard.
- Display VLM inference latency, frames analyzed per second, and alert counts.

### 10.2 Alert History Panel (Medium)

**Current Problem:** The bottom section of the dashboard shows "System Metrics & Logs Area" placeholder.

**Recommendation:**
- Implement an alert history timeline in this area.
- Show chronological alert events with timestamps, stream IDs, alert types, and severity.
- Add filtering and search capabilities.

### 10.3 Stream Status Indicators (Low)

**Recommendation:**
- Show stream connection status (connected/reconnecting/disconnected) in the stream list.
- Display actual FPS and resolution for each stream.
- Show VLM inference latency per stream.

### 10.4 Responsive Design & Accessibility (Low)

**Recommendations:**
- Test and fix layout on mobile/tablet form factors.
- Add ARIA labels to interactive elements.
- Ensure keyboard navigation works throughout the dashboard.
- Add color-blind-friendly alert indicators (not just red/green).

---

## 11. Priority Matrix

Recommendations ranked by impact and effort, grouped into implementation phases:

### Phase 1 — Critical Fixes (Week 1-2)

| # | Recommendation | Category | Impact | Effort |
|---|---------------|----------|--------|--------|
| 1 | Frame ingestion optimization (grab/retrieve throttling) | Performance | High | Low |
| 2 | Concurrent stream analysis (`asyncio.gather`) | Performance | High | Medium |
| 3 | Fix fire-and-forget task + graceful shutdown | Logic | High | Low |
| 4 | Bundle TailwindCSS locally (air-gap support) | Deployment | High | Low |
| 5 | Fix container name inconsistencies in docs | Documentation | Low | Low |

### Phase 2 — Core Improvements (Week 3-5)

| # | Recommendation | Category | Impact | Effort |
|---|---------------|----------|--------|--------|
| 6 | Pydantic request/response models for all endpoints | API | High | Medium |
| 7 | Add `/health`, `/ready`, `/metrics` endpoints | API | High | Low |
| 8 | Alert deduplication & state tracking | Agentic | High | Medium |
| 9 | VLM client retry logic + system prompt separation | Performance | Medium | Low |
| 10 | Configuration reference documentation | Documentation | Medium | Low |
| 11 | Add API authentication (API key) | Security | High | Medium |
| 12 | Remove hardcoded stream/agent limits | Scalability | Medium | Low |

### Phase 3 — Agentic Framework (Week 6-10)

| # | Recommendation | Category | Impact | Effort |
|---|---------------|----------|--------|--------|
| 13 | Google ADK integration — agent + tool registry | Agentic | Critical | High |
| 14 | Action tools (email, webhook, MQTT, snapshot) | Agentic | Critical | High |
| 15 | Alert severity & escalation logic | Agentic | Medium | Medium |
| 16 | Multi-agent coordination | Agentic | Medium | High |
| 17 | Agentic framework documentation | Documentation | High | Medium |
| 18 | Alert history endpoint + UI panel | API/UI | Medium | Medium |

### Phase 4 — Production Readiness (Week 11-14)

| # | Recommendation | Category | Impact | Effort |
|---|---------------|----------|--------|--------|
| 19 | Unit & integration test suite | Testing | High | High |
| 20 | CI/CD pipeline | Testing | Medium | Medium |
| 21 | Container hardening (no host networking, GPU passthrough) | Deployment | Medium | Medium |
| 22 | CORS + input sanitization | Security | Medium | Low |
| 23 | Per-stream configuration & zone management | Scalability | Medium | Medium |
| 24 | Architecture & developer guide documentation | Documentation | Medium | Medium |
| 25 | System metrics implementation in dashboard | UI | Medium | Medium |

---

*End of Review*
