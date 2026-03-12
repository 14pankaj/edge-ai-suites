# Get Started

This guide covers the rapid deployment of the Live Video Alert Agent system using Docker.

## Prerequisites

- Docker and Docker Compose
- Internet connection (for initial VLM model download)

## Initial Setup

1. **Clone the repository**:
     ```bash
     # Clone the latest on mainline
     git clone https://github.com/open-edge-platform/edge-ai-suites.git edge-ai-suites
     # Alternatively, clone a specific release branch
     git clone https://github.com/open-edge-platform/edge-ai-suites.git edge-ai-suites -b <release-tag>
     ```
    Note: Adjust the repo link appropriately in case of forked repo.

2. **Navigate to the Directory**:
     ```bash
     cd edge-ai-suites/metro-ai-suite/live-video-analysis/live-video-alert
     ```

3. **Configure Image Registry and Tag**:
     ```bash
     export REGISTRY="intel/"
     export TAG="latest"
     ```
    Skip this step if you prefer to build the sample application from source. For detailed instructions, refer to [How to Build from Source](./how-to-build-source.md) guide for details.

4. **Configure Environment**:

   **Core variables** (all optional — streams can also be added via UI after startup):
   ```bash
   # Pre-configure a video stream
   export RTSP_URL=rtsp://<camera-ip>:<port>/stream

   # VLM model selection (default: Phi-3.5-vision-instruct-int4-ov)
   export OVMS_SOURCE_MODEL=OpenVINO/InternVL2-2B-int4-ov
   export MODEL_NAME=InternVL2-2B

   # Application port (default: 9000)
   export PORT=9001

   # Log verbosity
   export LOG_LEVEL=DEBUG
   ```

   **API authentication** (leave empty to disable):
   ```bash
   export API_KEY=my-secret-key
   ```

   **Agentic dispatch — choose one mode:**

   *Option A — Google ADK (requires internet + Gemini API key):*
   ```bash
   export USE_ADK=true
   export GEMINI_API_KEY=<your-gemini-api-key>
   export ADK_MODEL=gemini-2.0-flash-lite   # default
   ```

   *Option B — Local LLM (fully offline, e.g. Ollama):*
   ```bash
   # Start Ollama: ollama run llama3.2
   export USE_LOCAL_LLM=true
   export LOCAL_LLM_URL=http://localhost:11434/v1
   export LOCAL_LLM_MODEL=llama3.2
   ```

   *Option C — Rule-based (default, no LLM needed):*
   ```bash
   # No extra variables required
   ```

   **Action tools** (configure the ones you want active):
   ```bash
   # Email notifications
   export SMTP_HOST=smtp.example.com
   export SMTP_PORT=587
   export SMTP_USER=alerts@example.com
   export SMTP_PASSWORD=<password>
   export SMTP_FROM=alerts@example.com
   export SMTP_ALERT_RECIPIENT=oncall@example.com

   # Webhook (receives HMAC-signed POST)
   export WEBHOOK_URL=https://hooks.example.com/alert
   export WEBHOOK_SECRET=<hmac-secret>          # optional

   # MQTT
   export MQTT_BROKER=192.168.1.20
   export MQTT_PORT=1883
   export MQTT_BASE_TOPIC=alerts/live-video
   ```

5. **Start the Application**:
   Run the following command from the project root:

     ```bash
     docker compose up -d
     ```

   **Note:**
   - First run downloads the VLM model (~2GB, 5-10 minutes)
   - An init container runs briefly to set up volume permissions.
   - Subsequent runs start instantly

6. **Verify Deployment**:
   Check that containers are running:
     ```bash
     docker ps
     ```

   View application logs:
     ```bash
     docker logs live-video-alert-agent
     ```

6. **Access the Dashboard**:
   Open your browser and navigate to:
     ```
     http://localhost:9000
     ```
   (Replace `localhost` with your server IP if accessing remotely)

## Using the Application

### Adding Video Streams
1. In the sidebar under **Stream Configuration**, enter:
   - **Stream Name**: A descriptive name (e.g., "Lobby Camera")
   - **RTSP URL**: Your camera's RTSP stream URL
2. Click **Add New Stream**

### Configuring Alerts
1. Under **AI Agent Alerts** section:
   - Click **Create New Alert**
   - Enter an **Alert Name** (e.g., "Fire Detection")
   - Write a **Prompt** describing the condition (e.g., "Is there fire or smoke?")
   - Set **Severity**, **Cooldown**, and the **Tools** to invoke on detection
2. Click **Save** to activate

   Alternatively, configure alerts via the REST API:
   ```bash
   curl -X POST http://localhost:9000/config/alerts \
     -H "Content-Type: application/json" \
     -H "X-API-Key: ${API_KEY}" \
     -d '[
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
     ]'
   ```

### Viewing Results
- The dashboard shows the live stream with analysis results below
- Use the dropdown to filter alerts: "All Alerts" or individual alert types
- Results update automatically via Server-Sent Events (SSE)
- The `alert_action` event surface shows which tools were invoked and whether escalation occurred

### Checking Health and Metrics

```bash
# Liveness
curl http://localhost:9000/health

# Readiness (non-200 = not ready)
curl http://localhost:9000/ready

# System + per-stream metrics
curl http://localhost:9000/metrics

# Query last 20 critical alert events
curl "http://localhost:9000/alerts/history?severity=critical&limit=20"

# List configured action tools
curl http://localhost:9000/tools
```

## Managing the Application

### Stopping Services

To stop all services:
```bash
docker compose down
```

### Restarting After Changes

```bash
# Restart both services
docker compose restart

# Restart only the application (VLM service keeps running)
docker compose restart live-video-alert-agent
```

### Viewing Logs

```bash
# Follow all logs
docker compose logs -f

# VLM service logs
docker logs -f ovms-vlm

# Application logs
docker logs -f live-video-alert-agent
```

### Clearing Model Cache

If you need to re-download the model or switch models:
```bash
# Remove everything including model cache
docker compose down -v

# Set environment and start fresh
export RTSP_URL=rtsp://<camera-ip>:<port>/stream
docker compose up -d
```

## Troubleshooting

### Permission Issues

**Problem**: OVMS fails with "permission denied" on `/models`.

**Solution**: An init container (`ovms-init`) automatically sets permissions. It will show as `Exited (0)` - this is normal.

**Verify**:
```bash
docker ps -a --filter "name=ovms-init"  # Should show: Exited (0)
docker exec ovms-vlm ls -lah /models    # Should be owned by ovms
```

### Other Issues

```bash
# Check status
docker compose ps

# View logs
docker compose logs -f

# Clean restart
docker compose down -v
export RTSP_URL=<your-url>
docker compose up -d
```
