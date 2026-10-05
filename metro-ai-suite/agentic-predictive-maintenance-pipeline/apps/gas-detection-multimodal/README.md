# Gas Detection (Multimodal) — Use Case

Fused image + sensor classification for the gas-detection dataset (Mendeley),
reusing logic ported from the upstream reference implementation
([intel/predictive-maintenance-pipeline](https://github.com/intel/predictive-maintenance-pipeline))
and plugged into this repo's detection-service, with the image branch now
running as a real DL Streamer `gvaclassify` pipeline (mirroring
`pipeline-defect-detection`'s `gvadetect` pipeline).

> **Use-case name vs. folder**: the use case is named `gas-detection` (pass
> `--use-case gas-detection` to `setup.sh`, `scripts/download_llm_model.sh`,
> and `scripts/download_and_prep_data.py`), while its files live in
> `apps/gas-detection-multimodal/`. The scripts locate the folder via its
> `.env_gas-detection` file; `configs/agents.yaml`'s `use_case_id` and the
> prompt file (`prompts/gas-detection.txt`) use the same name.

## Status: deployable via `setup.sh`, validated end-to-end

**What works today** (see `docs/user-guide/multimodal-pipeline-plan.md` for
full history):

- `configs/pipeline-server-config.json` — `gas_detection_multimodal`/`_gpu`/`_npu`
  pipeline definitions running `gvaclassify` (whole-frame classification,
  `inference-region=full-frame`) over the generated `datastream.mp4`,
  publishing per-frame results to a dedicated MQTT topic
  (`apm/gas-image-classifications`).
- `services/detection-service/src/utility/{sensor_classifier,fusion,
  dlstreamer_mqtt_collector}.py` — ported/unit-tested inference/fusion logic,
  plus an ephemeral MQTT collector that gathers one run's worth of per-frame
  classifications without ever writing them to storage-service on their own.
- `services/detection-service/src/utility/multimodal_runner.py` — config-driven
  orchestrator: runs the DL Streamer pipeline over the video once, re-joins
  each frame to its source image filename via `frame_manifest.json`
  (frame_id -> filename, written alongside the video by
  `scripts/download_and_prep_data.py`), classifies the paired sensor row,
  fuses per sample, and persists results to storage-service via the additive
  `image_confidence`, `sensor_confidence`, `sensor_raw_json`, `source` columns.
- `POST /detection/run-multimodal` on detection-service — same
  single-run-lock / "batch-complete" MQTT handoff contract as the existing
  `POST /detection/run` (video) path, so the agent-service reacts identically
  regardless of which path produced a batch.
- Trained models: `models/{image,sensor_mlp}/` (98.6% and 96.6% individual
  validation accuracy; 97.5% fused).
- Full stack deployable via `setup.sh --use-case gas-detection`:
  storage-service, detection-service (with the multimodal endpoint),
  agent-service (rule-based fallback reasoning), ui-service, nginx,
  dlstreamer-pipeline-server — all verified healthy.

**Not yet done**:

- UI (`ui-service`) has no dedicated surface for per-modality confidence yet
  — trigger a run via `curl` (see below) until that's built.
- The DL Streamer classification metadata schema (whole-frame ROI/tensor
  shape, and whether raw per-class probabilities are available over MQTT vs.
  only the top-1 label/confidence) should be confirmed against a real run in
  this stack; `multimodal_runner.py` falls back to an approximated
  probability vector from top-1 label/confidence if the full vector isn't
  present.

## Deploying

```bash
source setup.sh --use-case gas-detection
```

Then trigger a classification run (runs the DL Streamer image-classification
pipeline over the video once, fuses each frame with its paired sensor row,
and persists results):

```bash
curl -X POST http://localhost:8080/api/detection/run-multimodal \
     -H "Content-Type: application/json" \
     -d '{"device":"CPU","config_path":"/app/configs/gas_detection.docker.json"}'
```

Check status and results:

```bash
curl http://localhost:8080/api/detection/status/<run_id>
curl "http://localhost:8080/api/storage/detections?limit=10"
```

**Note**: after any code change to `detection-service` or `storage-service`,
rebuild before `up` — `setup.sh` does not pass `--build` automatically:

```bash
docker compose -f docker/compose.base.yaml -f docker/compose.telemetry.yaml \
  -f docker/compose.detection.yaml -f docker/compose.agents.yaml \
  -f docker/compose.ui.yaml build apm-detection apm-storage
```

(Run this in the same shell right after `source setup.sh ...` — the
`USE_CASE_MODELS_DIR`/`USE_CASE_CONFIGS_DIR`/`REGISTRY` env vars it exports do
not persist across separate shells/processes.)

## Regenerating the dataset

```bash
python scripts/download_and_prep_data.py \
    "https://data.mendeley.com/public-api/zip/zkwgkjkjn9/download/2" \
    --use-case gas-detection
```

This also (re)generates `resources/videos/datastream.mp4` and
`datasets/gas_detection/images/val/frame_manifest.json` (the frame-index ->
filename mapping the DL Streamer pipeline's per-frame results are re-joined
to sensor CSV rows through).

## Trying it locally (outside Docker)

Uses `configs/gas_detection.local.json` (repo-root-relative paths) instead of
`configs/gas_detection.docker.json` (container paths, used above). Requires
the DL Streamer Pipeline Server + MQTT broker to be reachable (e.g. via
`docker compose ... up dlstreamer-pipeline-server mqtt-broker`):

```python
import sys
sys.path.insert(0, "services/detection-service")
from src.utility.multimodal_runner import load_config, run_multimodal_classification

config = load_config("apps/gas-detection-multimodal/configs/gas_detection.local.json")
results = run_multimodal_classification(config, device="CPU")
for r in results:
    print(r["source"], r["label"], r["confidence"])
```

