# Known Issues

## Limited testing on EMT-S and EMT-D

- This release includes only limited testing on EMT‑S and EMT‑D. Some behaviors may not yet be fully validated across all scenarios.

## ADK mode requires internet access

Setting `USE_ADK=true` establishes a connection to Google’s Gemini API on every
alert dispatch. In air-gapped or restricted-network environments:

- Use `USE_LOCAL_LLM=true` with a locally hosted model instead.
- Or omit both flags to fall back to rule-based mode (no external LLM needed).

## Local LLM tool-calling support varies by model

Not all OVMS-served text models implement robust OpenAI-style function-calling.
The agent automatically falls back to JSON text parsing in that case, but very
small models (< 3B parameters) may produce unpredictable output.

Recommended models for reliable tool-calling: `Phi-4-mini-instruct`,
`Phi-3.5-mini-instruct`, `Mistral-7B-Instruct` (OV-converted variants).

If neither strategy returns valid tool names, rule-based dispatch is used as a
final fallback — alerts continue to function.

## Email tool silently skipped when SMTP not configured

If `send_email` is listed in an alert’s `tools` array but `SMTP_HOST` is not set,
the tool logs a warning and returns without error. Verify SMTP settings with:
```bash
curl -X POST http://localhost:9000/tools/send_email/invoke \
  -H "X-API-Key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"parameters": {"subject": "test", "body": "test", "stream_id": "cam1", "alert_name": "test", "severity": "low"}}'
```

## Snapshot directory not writable

If `capture_snapshot` fails with a permission error, the container’s
`/app/snapshots` directory may not be writable by `appuser`:
```bash
docker exec live-video-alert ls -la /app/snapshots
```
If using a host-bind mount instead of the `snapshots` named volume, ensure the
host directory is owned by UID 1000.

## API config endpoint renamed

As of 2.0.0, `/config/agents` is renamed to `/config/alerts`. Clients using the
old path will receive a 404. Update integrations accordingly.

## RTSP stream not connecting

Symptoms:
- Stream shows "No streams active" or fails to add via UI.
- Video feed shows a black screen or connection timeout.

Checks:
- Verify RTSP URL is reachable and credentials are correct.
- Ensure firewall allows RTSP port (default 554).
- Test with local file: `file:///path/to/video.mp4`.

## SSE events not updating

Symptoms:
- Dashboard shows stale data, or the "Last Sync" timestamp doesn't update.
- Alert results don't appear in real-time.

Checks:
- Check browser console (F12) for connection errors.
- Verify that OVMS is running: `docker logs ovms-vlm | grep "Started REST"`.
- Test endpoint: `curl -N http://localhost:9000/events`.
- Ensure port 9000 isn't blocked by firewall.

## Port conflicts

If the dashboard or APIs are not reachable, check whether port 9000 is already in use and update the environment variable:
```bash
export PORT=9001
docker compose down && docker compose up -d
```

## VLM validation errors

Symptoms:
- Logs show "Validation failed" or "JSON parse error".
- Alerts show "NO" with reason "Validation error".

Checks:
- Verify model loaded: `docker logs ovms-vlm | grep "AVAILABLE"`.
- Simplify prompts to ask clear yes/no questions.
- Reduce concurrent alerts (max 4).

## Performance/throughput lower than expected

- Use faster model: `export OVMS_SOURCE_MODEL=OpenVINO/InternVL2-1B-int4-ov`.
- Reduce active streams or increase `ANALYSIS_INTERVAL`.
- Ensure hardware meets minimum requirements (see [system-requirements.md](./system-requirements.md)).

## Model download fails

Symptoms:
- The OVMS container exits or fails to start.
- Logs show Hugging Face download errors.

Checks:
- Check internet connectivity and proxy settings (`http_proxy`, `https_proxy`).
- Set the `HF_TOKEN` environment variable for gated models.
- Ensure 2-4GB disk space available.
- Verify: `docker ps -a | grep ovms-init` shows "Exited (0)".
