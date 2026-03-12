# How It Works

The Live Video Alert Agent is a multi-layered agentic application that ingests RTSP
video streams, applies VLM-based scene understanding, and dispatches configurable
actions through an agentic tool-calling pipeline.

## Architecture Overview

```text
RTSP Sources (N cameras)
     │
     ▼
LiveStreamManager × N          grab()/retrieve() throttled decode
     │                         exponential-backoff reconnection
     │  frame (latest)
     ▼
AgentManager                   one asyncio.Task per stream (concurrent)
  ├─ VlmClient ──────────────► OVMS / OpenAI-compatible VLM
  │   └─ retry + backoff       Phi-3.5-Vision | InternVL2-2B ...
  │
  ├─ AlertStateManager         per-stream × per-alert runtime state
  │   ├─ cooldown gate         suppresses repeat firings
  │   ├─ consecutive counter   detects persistent conditions
  │   └─ escalation trigger    promotes alert tier after N consecutives
  │
  ├─ AlertActionAgent          decides WHICH tools to call
  │   ├─ ADK mode              Google ADK LlmAgent + FunctionTool
  │   ├─ Local LLM mode        OVMS-hosted text model endpoint
  │   └─ Rule-based mode       direct tool execution — no LLM needed
  │
  └─ Action Tools (async)
      ├─ log_alert              structured log + in-memory history
      ├─ capture_snapshot       JPEG frame to disk / named volume
      ├─ send_email             aiosmtplib SMTP notification
      ├─ trigger_webhook        HMAC-signed HTTP POST
      └─ publish_mqtt           paho-mqtt 2.x MQTTv5 publish
     │
     ▼
EventManager (SSE pub/sub)     alerts fan-out to all connected browsers
     │
     ▼
Dashboard UI                   real-time stream tiles, alert feed, history
```

## Key Components

### LiveStreamManager

Each registered camera has its own `LiveStreamManager` running in a daemon thread.

- Uses `cv2.VideoCapture.grab()` followed by `retrieve()` to skip deep-decode on
  unused frames, reducing CPU usage proportionally to the gap between capture FPS
  and analysis FPS.
- Frame interval is controlled by `CAPTURE_FPS` (default: auto-derived from
  `ANALYSIS_INTERVAL`).
- Reconnects on drop-out with exponential back-off (2 s → 30 s).
- Exposes a `get_health()` method returning connection status, actual FPS,
  resolution, and buffer fill level.

### AgentManager

The central orchestrator. Instead of a single serial loop across all cameras, each
stream gets an independent `asyncio.Task`:

```text
add_stream("cam1", ...) → _launch_stream_task("cam1")
add_stream("cam2", ...) → _launch_stream_task("cam2")

cam1-task: _stream_analysis_loop() running every ANALYSIS_INTERVAL seconds
cam2-task: _stream_analysis_loop() running every ANALYSIS_INTERVAL seconds
```

Failed or cancelled tasks are automatically restarted via an `add_done_callback`.

### VlmClient

Thin async wrapper around `openai.AsyncOpenAI`, targeting OVMS (OpenVINO Model
Server) via its OpenAI-compatible REST API.

- Sends a `system` role message (VLM system instruction) plus a `user` message
  containing the base64-encoded frame and the structured alert prompt.
- Retries failed calls up to `VLM_MAX_RETRIES` times with exponential back-off.
- Alert prompts are serialised with `json.dumps` — not f-strings — to prevent
  prompt-injection from user-supplied alert names or text.

### AlertStateManager

Maintains per-stream × per-alert runtime state without any database dependency:

| State field | Purpose |
|---|---|
| `last_action_time` | Cooldown gate — suppresses actions until `cooldown_seconds` elapses |
| `consecutive_yes` | Counts unbroken YES detections; triggers escalation |
| `last_answer` | Detects state transitions (NO→YES, YES→NO) |
| `history` (ring-buffer) | Stores `AlertEvent` records up to `ALERT_HISTORY_SIZE` |

`process()` returns `(should_act, is_escalation, is_transition)` so the manager
can decide whether to invoke tools and which tier of tools to use.

### AlertActionAgent

Decides which tools to invoke for a fired alert. Operates in one of three modes,
selected automatically at startup:

#### Mode 1 — Google ADK (`USE_ADK=true`)

Uses Google's Agent Development Kit with a `LlmAgent` (Gemini) that receives
structured alert context and calls `FunctionTool`-wrapped async tool functions.
Best for dynamic, LLM-reasoned escalation logic that can be adjusted without code
changes.

Requires: `GEMINI_API_KEY` environment variable.

#### Mode 2 — Local LLM (`USE_LOCAL_LLM=true`)

Connects to an OVMS-hosted OpenAI-compatible text endpoint. Two-tier execution:

1. **Tool-calling API** — sends `tools=` schemas; models that support
   function-calling (llama3.1+, Mistral, Phi-3, etc.) return `tool_calls` directly.
2. **JSON text fallback** — re-prompts asking for a JSON array of tool names;
   a regex+JSON parser extracts valid names from free-form text.

Requires: `LOCAL_LLM_URL` and `LOCAL_LLM_MODEL`.

#### Mode 3 — Rule-based (default)

Directly executes the tool list from `AlertConfig.tools` in order. No external LLM
required — works fully offline and air-gapped. Escalation tools from
`AlertConfig.escalation.additional_tools` are appended when the consecutive
threshold is reached.

### Action Tools

All five tools are async functions registered in `_TOOL_MAP`:

| Tool | Trigger condition | Configuration |
|---|---|---|
| `log_alert` | Always | Built-in, always active |
| `capture_snapshot` | Alert fires | `SNAPSHOT_DIR` writable |
| `send_email` | Alert fires | `SMTP_HOST` set |
| `trigger_webhook` | Alert fires | `WEBHOOK_URL` set |
| `publish_mqtt` | Alert fires | `MQTT_BROKER` set |

Tools are configured per-alert in `AlertConfig.tools` and are silently skipped
if their required env var is not set.

### Alert Configuration Schema

Each alert is described by an `AlertConfig` Pydantic model:

```json
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
}
```

| Field | Values | Description |
|---|---|---|
| `severity` | `low` \| `medium` \| `high` \| `critical` | Drives ADK/local-LLM tool selection |
| `cooldown_seconds` | ≥ 0 | Minimum gap between repeated alert actions |
| `tools` | list of tool names | Tools invoked when alert fires |
| `escalation.threshold_consecutive` | integer | Consecutive YES count before escalation |
| `escalation.additional_tools` | list of tool names | Extra tools added on escalation |

## Event Types

The SSE stream (`GET /events`) emits four event types:

| Event | When |
|---|---|
| `init` | On SSE connect — current streams + latest results |
| `analysis` | Each VLM analysis cycle completes |
| `alert_action` | Alert fired and tools were invoked |
| `keepalive` | Every 15 s to prevent proxy timeouts |
