# System Requirements

This page summarizes the recommended environment for running Live Video Alert Agent.

## Operating Systems

- Ubuntu 24.04.1 LTS

## Minimum Requirements

| **Component**       | **Minimum**                     | **Recommended**                                  |
|---------------------|---------------------------------|--------------------------------------------------|
| **Processor**       | 11th Gen Intel® Core™ Processor | Intel® Xeon® Platinum 8351N CPU @ 2.40GHz        |
| **Memory**          | 16 GB                           | 32 GB                                            |
| **Disk Space**      | 256 GB SSD                      | 256 GB SSD                                       |
| **GPU/Accelerator** | Intel® UHD Graphics             | Intel® Arc™ Graphics                             |

## Software Requirements

- Docker Engine and Docker Compose
- Intel® Graphics compute runtime (if using Intel GPU for inference acceleration)
- RTSP source reachable from the `live-video-alert-agent` container (optional, can be added via UI)

## Network / Ports

Default ports (configurable via environment variables):

- `PORT=9000` (Dashboard UI and REST API)

## Model Requirements

The application automatically downloads VLM models on first run (~2 GB). Supported models:

- `OpenVINO/Phi-3.5-vision-instruct-int4-ov` (default)
- `OpenVINO/InternVL2-2B-int4-ov` (alternative)

Configure via environment variables:
```bash
export OVMS_SOURCE_MODEL=OpenVINO/InternVL2-2B-int4-ov
export MODEL_NAME=InternVL2-2B
```

## Optional: Local LLM for Agentic Dispatch

To use `USE_LOCAL_LLM=true` without installing a separate service, a text-only
model can be served alongside the vision model by a second OVMS instance, or
you can use any OpenAI-compatible server on the same host:

| Backend | Default URL |
|---|---|
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| vLLM | `http://localhost:8080/v1` |
| OVMS text model | `http://localhost:8001/v3` |

No additional Python packages are required — the `openai` SDK is already included.

## Validation

Proceed to [Get Started](./get-started.md) once Docker is installed and internet
connectivity is available for model downloads.
