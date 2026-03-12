# Release Notes

## 2.0.0
### Release Date: 12 Mar 2026

#### Agentic Framework
- **Google ADK integration**: Set `USE_ADK=true` + `GEMINI_API_KEY` to enable
  LLM-driven tool-call reasoning via `LlmAgent` + `FunctionTool`.
- **Local LLM support**: Set `USE_LOCAL_LLM=true` + `LOCAL_LLM_URL` +
  `LOCAL_LLM_MODEL` to use an OVMS-hosted text model endpoint.
  Supports tool-calling API with JSON-text fallback.
- **Rule-based mode** (default): Fully offline, no LLM required.
- Three-tier dispatch priority: ADK > Local LLM > Rule-based.

#### Action Tools (5 new async tools)
- `log_alert` — structured log entry + in-memory ring-buffer history
- `capture_snapshot` — saves current frame as JPEG to `SNAPSHOT_DIR`
- `send_email` — SMTP notification via `aiosmtplib`
- `trigger_webhook` — HMAC-SHA256-signed HTTP POST via `aiohttp`
- `publish_mqtt` — MQTTv5 publish via `paho-mqtt`

#### Alert State Management
- Per-stream × per-alert cooldown gate (`cooldown_seconds`)
- Consecutive YES counter with automatic escalation support
- In-memory alert history ring-buffer (configurable `ALERT_HISTORY_SIZE`)
- New `AlertConfig` Pydantic schema with `severity`, `tools`, and `escalation` fields

#### Multi-Camera Concurrency
- Each stream runs in its own independent `asyncio.Task`
- Auto-restart on task failure via `add_done_callback`
- `grab()`/`retrieve()` frame throttling reduces CPU usage on idle streams

#### New API Endpoints
- `GET /health` — liveness probe with uptime and stream counts
- `GET /ready` — readiness probe
- `GET /metrics` — CPU/memory + per-stream inference counters
- `GET /alerts/history` — queryable alert history
- `GET /tools` — list registered tools and their enabled status
- `POST /tools/{name}/invoke` — manual tool testing
- `GET /streams` now returns full health status per stream

#### Breaking Changes
- `GET /config/agents` → renamed to `GET /config/alerts`
- `POST /config/agents` → renamed to `POST /config/alerts`
- Alert config schema extended (new required/optional fields: `severity`,
  `cooldown_seconds`, `tools`; the old minimal `{name, prompt, enabled}` format
  is still accepted with defaults applied)
- The hardcoded 4-alert limit has been removed

#### Dependencies Added
- `google-adk>=0.3.0` — ADK agent framework
- `aiohttp>=3.9.0` — async webhook HTTP client
- `aiosmtplib>=3.0.0` — async SMTP email
- `paho-mqtt>=2.0.0` — MQTT action tool
- `psutil>=5.9.0` — system metrics endpoint

#### Infrastructure
- `Dockerfile`: `HEALTHCHECK` added (uses `/health` endpoint); `curl` included in
  runtime image; `snapshots/` and `config/` directories pre-created with correct
  ownership.
- `docker-compose.yml`: `snapshots` named volume; healthcheck on `live-video-alert`
  service; all new env vars forwarded with sensible defaults.

---

## 1.0.0-rc.0
### Release Date: 17 Feb 2026

- Initial release of Live Video Alert
- RTSP video ingestion with VLM inference (Phi-3.5-Vision, InternVL2-2B)
- Natural language alert configuration (max 4 alerts per stream)
- Real-time SSE event broadcasting and interactive dashboard
- Helm support is not available in this version.