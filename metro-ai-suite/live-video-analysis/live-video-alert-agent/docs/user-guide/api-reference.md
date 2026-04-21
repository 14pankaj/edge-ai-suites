# API Reference

The Live Video Alert Agent exposes REST and SSE endpoints for management,
data consumption, and operational monitoring.


## Observability

### `GET /health`
Liveness probe. Always returns `200` if the application process is alive.
- **Response**:
  ```json
  {
    "status": "healthy",
    "streams_active": 2,
    "agents_enabled": 3,
    "vlm_reachable": true,
    "uptime_seconds": 342.1,
    "timestamp": "2026-03-12T10:00:00Z"
  }
  ```

### `GET /ready`
Readiness probe. Returns `200` only when the manager is running and at least
one alert is enabled. Returns `503` otherwise.
- **Response** (ready):
  ```json
  {"status": "ready", "streams": 1, "alerts": 2}
  ```

### `GET /metrics`
System and per-stream inference counters.
- **Response**:
  ```json
  {
    "cpu_percent": 18.4,
    "memory_percent": 42.1,
    "streams": [
      {
        "stream_id": "cam1",
        "analysis_count": 120,
        "alert_count": 5,
        "last_inference_ms": 850.3
      }
    ]
  }
  ```

---

## Streaming

### `GET /events` (SSE)
Server-Sent Events stream for real-time updates.
- **Response Type**: `text/event-stream`
- **Events**:

| Event | When | Data fields |
|---|---|---|
| `init` | On connect | `results`, `streams` |
| `analysis` | Each VLM cycle | `stream_id`, `results` |
| `alert_action` | Alert fired + tools invoked | `stream_id`, `alert_name`, `severity`, `answer`, `reason`, `actions_taken`, `escalated`, `snapshot_path` |
| `keepalive` | Every 15 s | `ts` |

- **`alert_action` example**:
  ```json
  {
    "event": "alert_action",
    "data": {
      "stream_id": "cam1",
      "alert_name": "Fire Detection",
      "severity": "critical",
      "answer": "YES",
      "reason": "Flames visible in lower-left quadrant",
      "actions_taken": ["log_alert", "capture_snapshot", "send_email"],
      "escalated": false,
      "snapshot_path": "/app/snapshots/cam1/Fire_Detection_critical_20260312T100512.jpg"
    }
  }
  ```

### `GET /video_feed`
MJPEG stream for live frame preview.
- **Query Parameters**: `stream_id` (string, default: `default`)
- **Response Type**: `multipart/x-mixed-replace; boundary=frame`

### `GET /data`
Legacy polling endpoint for backward compatibility. Prefer `/events` for new
integrations.
- **Response**: `{"<stream_id>": {"<alert_name>": {"answer": ..., "reason": ...}}}`

---

## Stream Management

### `GET /streams`
List all active streams with health status.
- **Response**:
  ```json
  {
    "streams": [
      {
        "id": "cam1",
        "url": "rtsp://192.168.1.10:554/stream",
        "connected": true,
        "fps": 1.0,
        "resolution": "1920x1080",
        "buffer_fill": 1
      }
    ]
  }
  ```

### `POST /streams`  `🔑 auth`
Register a new video stream.
- **Request Body**:
  ```json
  {"id": "cam1", "url": "rtsp://192.168.1.10:554/stream"}
  ```
- **Response**: `{"status": "added", "id": "cam1"}`
- **Status Codes**: `200` added | `409` already exists | `422` validation error | `401` missing API key

### `DELETE /streams/{stream_id}`  `🔑 auth`
Remove an active stream.
- **Response**: `{"status": "removed", "id": "cam1"}`
- **Status Codes**: `200` removed | `404` not found | `401` missing API key

---

## Alert Configuration

### `GET /config/alerts`
Return the current alert configurations.
- **Response**: array of `AlertConfig` objects (see schema below).

### `POST /config/alerts`  `🔑 auth`
Replace the full alert configuration.
- **Request Body**: array of alert config objects:
  ```json
  [
    {
      "name": "Fire Detection",
      "prompt": "Is there fire or smoke visible?",
      "enabled": true,
      "severity": "critical",
      "cooldown_seconds": 60,
      "tools": ["log_alert", "capture_snapshot", "send_email"],
      "escalation": {
        "threshold_consecutive": 3,
        "additional_tools": ["trigger_webhook", "publish_mqtt"]
      }
    },
    {
      "name": "Person Detection",
      "prompt": "Is there a person in the frame?",
      "enabled": true,
      "severity": "medium",
      "cooldown_seconds": 30,
      "tools": ["log_alert"]
    }
  ]
  ```
- **Alert Config Fields**:

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Alert identifier (alphanumeric, spaces, hyphens) |
| `prompt` | string | yes | Natural-language yes/no question sent to the VLM |
| `enabled` | bool | no (default `true`) | Whether this alert is active |
| `severity` | `low`\|`medium`\|`high`\|`critical` | no (default `medium`) | Severity level; drives ADK/LLM tool selection |
| `cooldown_seconds` | float ≥ 0 | no (default `60`) | Minimum interval between repeated alert actions |
| `tools` | list of tool names | no (default `["log_alert"]`) | Tools to invoke when alert fires |
| `escalation.threshold_consecutive` | int ≥ 1 | no | Consecutive YES count before escalation |
| `escalation.additional_tools` | list of tool names | no | Extra tools added on escalation |

- **Valid tool names**: `log_alert`, `capture_snapshot`, `send_email`, `trigger_webhook`, `publish_mqtt`
- **Response**: `{"status": "saved", "count": 2}`
- **Status Codes**: `200` saved | `422` schema validation failed | `401` missing API key

---

## Alert History

### `GET /alerts/history`
Query the in-memory alert detection history (newest-first).
- **Query Parameters**:

| Parameter | Type | Description |
|---|---|---|
| `stream_id` | string | Filter by stream |
| `alert_name` | string | Filter by alert name |
| `severity` | string | Filter by severity (`low`\|`medium`\|`high`\|`critical`) |
| `answer` | string | Filter by VLM answer (`YES`\|`NO`) |
| `limit` | int (1–500, default 50) | Maximum results to return |

- **Response**:
  ```json
  {
    "count": 2,
    "events": [
      {
        "event_id": "abc123",
        "stream_id": "cam1",
        "alert_name": "Fire Detection",
        "severity": "critical",
        "answer": "YES",
        "reason": "Flames visible",
        "timestamp": "2026-03-12T10:05:12Z",
        "actions_taken": ["log_alert", "capture_snapshot", "send_email"],
        "escalated": false,
        "snapshot_path": "/app/snapshots/cam1/Fire_Detection_critical_20260312T100512.jpg"
      }
    ]
  }
  ```

---

## Action Tools

### `GET /tools`
List all registered action tools and whether they are currently enabled (based
on environment variable configuration).
- **Response**:
  ```json
  {
    "tools": [
      {"name": "log_alert", "description": "...", "enabled": true},
      {"name": "send_email", "description": "...", "enabled": false}
    ]
  }
  ```

### `POST /tools/{tool_name}/invoke`  `🔑 auth`
Manually invoke a registered tool for testing.
- **Path Parameters**: `tool_name` — one of `log_alert`, `send_email`,
  `trigger_webhook`, `capture_snapshot`, `publish_mqtt`
- **Request Body**:
  ```json
  {"parameters": {"stream_id": "cam1", "alert_name": "Test", "severity": "low"}}
  ```
- **Response**:
  ```json
  {
    "tool": "log_alert",
    "status": "success",
    "result": {"status": "logged"},
    "duration_ms": 1.2
  }
  ```
- **Status Codes**: `200` (success or error both return 200 with `status` field) | `404` tool not found | `401` missing API key

---

## Dashboard UI

### `GET /`
Serves the monitoring dashboard HTML.
- **Response**: `text/html`
