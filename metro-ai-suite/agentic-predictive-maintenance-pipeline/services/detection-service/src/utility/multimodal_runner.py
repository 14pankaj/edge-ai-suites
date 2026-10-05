# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Config-driven orchestration of a fused image + sensor classification run.

Ties together a real DL Streamer ``gvaclassify`` video pipeline (the image
branch), ``SensorMLPClassifier`` (the sensor branch), and
``fusion.late_fusion()`` for a single batch: run the use case's video through
DL Streamer once (whole-frame classification per frame), classify the paired
sensor readings, fuse the two per sample, and persist each fused result to
storage-service.

The image branch used to call a direct-OpenVINO ``ImageClassifier`` over a
static folder of images; it now reuses
``dlstreamer_client.run_pipeline_to_completion`` (the same bounded-run/poll
logic ``pipeline-defect-detection`` uses) plus a dedicated, ephemeral MQTT
collector (``dlstreamer_mqtt_collector.py``) so per-frame classifications
never get durably persisted on their own — only the final fused result does.
Since DL Streamer only reports a frame id (not the original filename), a
``frame_manifest.json`` written alongside the source video by
``scripts/download_and_prep_data.py`` re-joins frame id -> filename -> sensor
CSV row.

Use case configs (sensor model paths, dataset paths, class names, fusion
weights, ``frame_manifest_path``, optional ``video_fps``/``mqtt_topic``/
``video_filename``) are loaded from a JSON file — see
``apps/gas-detection-multimodal/configs/gas_detection.docker.json`` for the
reference config.
"""

import json
import logging
from pathlib import Path

from . import dlstreamer_client
from .dlstreamer_mqtt_collector import CollectorError, GasClassificationCollector
from .fusion import late_fusion
from .sensor_classifier import SensorMLPClassifier

log = logging.getLogger(__name__)


class MultimodalRunError(RuntimeError):
    """Raised when a multimodal classification run cannot complete."""


def load_config(config_path: str) -> dict:
    """Load and lightly validate a multimodal use-case config file."""
    path = Path(config_path)
    if not path.is_file():
        raise MultimodalRunError(f"Multimodal config not found: {config_path}")
    with open(path) as f:
        config = json.load(f)

    required = {"frame_manifest_path", "sensor_model_path",
                "sensor_data_path", "feature_columns", "join_column",
                "class_names", "fusion_weights"}
    missing = required - config.keys()
    if missing:
        raise MultimodalRunError(f"Multimodal config {config_path} missing keys: {sorted(missing)}")
    return config


def _load_frame_manifest(manifest_path: str) -> dict[int, str]:
    """Load the frame_index -> image filename manifest written alongside the
    DL Streamer source video by ``scripts/download_and_prep_data.py``."""
    path = Path(manifest_path)
    if not path.is_file():
        raise MultimodalRunError(f"Frame manifest not found: {manifest_path}")
    with open(path) as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


def _image_probs_from_classifications(
    classifications: list[dict], manifest: dict[int, str], class_names: dict[int, str]
) -> dict[str, list[float]]:
    """Map each collected per-frame classification back to its source image
    filename (via ``manifest``) and to a per-class probability vector.

    Prefers the DL Streamer classifier's raw per-class ``probabilities`` when
    present; otherwise approximates a probability vector from the top-1
    ``label``/``confidence`` (predicted class gets ``confidence``, the rest
    share ``1 - confidence`` uniformly) since ``gvaclassify`` may only expose
    the winning label, not the full softmax vector.
    """
    ordered_labels = [class_names[i] for i in sorted(class_names)]
    n_classes = len(ordered_labels)
    label_to_index = {label: i for i, label in enumerate(ordered_labels)}

    image_probs: dict[str, list[float]] = {}
    for record in classifications:
        filename = manifest.get(record["frame_id"])
        if filename is None:
            log.warning("No manifest entry for frame_id=%s — skipping", record["frame_id"])
            continue

        if record.get("probabilities") and len(record["probabilities"]) == n_classes:
            image_probs[filename] = list(record["probabilities"])
            continue

        idx = label_to_index.get(record["label"])
        confidence = record.get("confidence", 0.0)
        if idx is None:
            image_probs[filename] = [1.0 / n_classes] * n_classes
            continue
        remainder = (1.0 - confidence) / (n_classes - 1) if n_classes > 1 else 0.0
        vec = [remainder] * n_classes
        vec[idx] = confidence
        image_probs[filename] = vec

    return image_probs


def run_multimodal_classification(config: dict, device: str = "CPU") -> list[dict]:
    """Classify every video frame (+ its paired sensor row) described by ``config``.

    Unlike the original direct-OpenVINO-over-a-folder implementation, the
    image branch now runs as a real DL Streamer ``gvaclassify`` pipeline over
    the use case's generated video (mirroring ``pipeline-defect-detection``'s
    detection pipeline): a bounded run publishes per-frame whole-frame
    classifications to a dedicated MQTT topic (not the shared
    ``apm/detections`` topic), which this function collects for the run's
    duration, then re-joins to the original image filename via
    ``frame_manifest.json`` (frame_id -> filename, written in the same frame
    order the video was built with) so it can still fuse with the paired
    sensor CSV row (keyed by filename) exactly as before.

    Returns one fused result dict per sample: ``source`` (image filename),
    ``label``, ``confidence``, ``label_id``, ``probabilities``,
    ``image_confidence``, ``sensor_confidence``, ``sensor_raw_json``.
    """
    class_names = {int(k): v for k, v in config["class_names"].items()}
    manifest = _load_frame_manifest(config["frame_manifest_path"])

    collector = GasClassificationCollector(
        topic=config.get("mqtt_topic"),
        fps=float(config.get("video_fps", 30)),
    )
    try:
        collector.start()
    except CollectorError as exc:
        raise MultimodalRunError(str(exc)) from exc

    completed = False
    try:
        dlstreamer_client.run_pipeline_to_completion(
            device=device,
            video_filename=config.get("video_filename"),
            mqtt_topic=collector.topic,
        )
        completed = True
    finally:
        # Only wait for trailing in-flight frames after a successful run.
        classifications = collector.stop(expected_count=len(manifest) if completed else None)

    if not classifications:
        raise MultimodalRunError(
            "No image classifications collected from the DL Streamer pipeline run"
        )

    image_probs = _image_probs_from_classifications(classifications, manifest, class_names)
    if not image_probs:
        raise MultimodalRunError("No collected classifications could be matched to a manifest frame")
    sample_ids = list(image_probs.keys())

    sensor_clf = SensorMLPClassifier(
        model_path=config["sensor_model_path"],
        data_path=config["sensor_data_path"],
        feature_columns=config["feature_columns"],
        join_column=config["join_column"],
        device=device,
    )
    sensor_clf.load()
    sensor_keys = [SensorMLPClassifier.sample_key_from_image_name(s) for s in sample_ids]
    sensor_results = sensor_clf.infer(sensor_keys, n_classes=len(class_names))
    sensor_probs = {sample_ids[i]: r["probabilities"] for i, r in enumerate(sensor_results)}
    metadata = {
        sample_ids[i]: {k: v for k, v in r.items() if k == "sensor_raw_json"}
        for i, r in enumerate(sensor_results)
    }

    return late_fusion(
        branch_probs={"image": image_probs, "sensor": sensor_probs},
        fusion_weights=config["fusion_weights"],
        sample_ids=sample_ids,
        class_names=class_names,
        metadata_by_sample=metadata,
    )


def persist_results(results: list[dict], source_tag: str, post_detection_fn) -> int:
    """Persist each fused result via ``post_detection_fn`` (a callable taking
    the same payload shape as ``storage_client.post_detection``).

    Multimodal results have no bounding box, so x/y/width/height are 0.0.
    ``frame_id`` is a per-run incrementing index (0-based), since these
    samples come from a static dataset rather than a video frame sequence.
    """
    inserted = 0
    for frame_id, result in enumerate(results):
        payload = {
            "frame_id": frame_id,
            "label": result["label"],
            "confidence": result["confidence"],
            "x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0,
            "source": source_tag,
            "image_confidence": result.get("image_confidence"),
            "sensor_confidence": result.get("sensor_confidence"),
            "sensor_raw_json": result.get("sensor_raw_json"),
        }
        try:
            post_detection_fn(payload)
            inserted += 1
        except Exception as exc:
            log.warning("Could not persist multimodal result for %s: %s", result.get("source"), exc)
    return inserted
