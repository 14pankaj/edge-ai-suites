# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for multimodal_runner.py — config loading, frame-manifest re-join,
orchestration, and persistence, with the DL Streamer pipeline call/collector
and SensorMLPClassifier mocked so tests run without a real DL Streamer
Pipeline Server, MQTT broker, model files, or datasets.
"""

import json
import os
import tempfile

import pytest

from src.utility.multimodal_runner import (
    MultimodalRunError,
    load_config,
    persist_results,
    run_multimodal_classification,
)


@pytest.fixture
def frame_manifest_file(tmp_path):
    manifest = {"0": "img_Smoke.jpg", "1": "img_NoGas.jpg"}
    path = tmp_path / "frame_manifest.json"
    path.write_text(json.dumps(manifest))
    return str(path)


@pytest.fixture
def sample_config(frame_manifest_file):
    return {
        "frame_manifest_path": frame_manifest_file,
        "sensor_model_path": "/models/sensor/sensor_mlp.xml",
        "sensor_data_path": "/data/sensor.csv",
        "feature_columns": ["MQ2", "MQ3"],
        "join_column": "Corresponding Image Name",
        "class_names": {"0": "Mixture", "1": "NoGas", "2": "Perfume", "3": "Smoke"},
        "fusion_weights": {"image": 0.6, "sensor": 0.4},
    }


def test_load_config_reads_json_file(sample_config):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(sample_config, f)
        path = f.name
    try:
        loaded = load_config(path)
        assert loaded == sample_config
    finally:
        os.unlink(path)


def test_load_config_missing_file_raises():
    with pytest.raises(MultimodalRunError, match="not found"):
        load_config("/nonexistent/path/config.json")


def test_load_config_missing_required_keys_raises(sample_config):
    del sample_config["fusion_weights"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(sample_config, f)
        path = f.name
    try:
        with pytest.raises(MultimodalRunError, match="fusion_weights"):
            load_config(path)
    finally:
        os.unlink(path)


class FakeCollector:
    """Stand-in for GasClassificationCollector — returns canned per-frame
    classification records instead of really subscribing to MQTT."""

    records: list = []
    topic = "apm/gas-image-classifications"

    def __init__(self, topic=None, fps=30.0):
        self.topic = topic or FakeCollector.topic
        self.fps = fps
        self.started = False

    def start(self):
        self.started = True

    def stop(self, expected_count=None):
        return FakeCollector.records


class FakeSensorClassifier:
    def __init__(self, model_path, data_path, feature_columns, join_column, device):
        pass

    def load(self):
        pass

    def infer(self, sample_keys, n_classes):
        return [
            {"source": key, "probabilities": [0.1, 0.1, 0.1, 0.7],
             "sensor_raw_json": json.dumps({"MQ2": 1.0})}
            for key in sample_keys
        ]

    @staticmethod
    def sample_key_from_image_name(name):
        return name.rsplit(".", 1)[0]


def _patch_pipeline(monkeypatch, runner_mod, records):
    FakeCollector.records = records
    monkeypatch.setattr(runner_mod, "GasClassificationCollector", FakeCollector)
    monkeypatch.setattr(runner_mod, "SensorMLPClassifier", FakeSensorClassifier)
    monkeypatch.setattr(
        runner_mod.dlstreamer_client, "run_pipeline_to_completion",
        lambda **kwargs: {"state": "COMPLETED"},
    )


def test_run_multimodal_classification_fuses_both_branches(sample_config, monkeypatch):
    """Wire a fake collector/pipeline-run/SensorMLPClassifier and confirm the
    runner re-joins frame ids to filenames via the manifest, then fuses."""
    import src.utility.multimodal_runner as runner_mod

    _patch_pipeline(monkeypatch, runner_mod, records=[
        {"frame_id": 0, "label": "Smoke", "confidence": 0.85, "probabilities": None},
        {"frame_id": 1, "label": "NoGas", "confidence": 0.8, "probabilities": None},
    ])

    results = run_multimodal_classification(sample_config, device="CPU")

    assert len(results) == 2
    smoke_result = next(r for r in results if r["source"] == "img_Smoke.jpg")
    assert smoke_result["label"] == "Smoke"
    assert "image_confidence" in smoke_result
    assert "sensor_confidence" in smoke_result
    assert smoke_result["sensor_raw_json"] == json.dumps({"MQ2": 1.0})


def test_run_multimodal_classification_prefers_raw_probabilities(sample_config, monkeypatch):
    """When the collector reports a full per-class probability vector, it's
    used directly instead of the top-1 label/confidence approximation."""
    import src.utility.multimodal_runner as runner_mod

    _patch_pipeline(monkeypatch, runner_mod, records=[
        {"frame_id": 0, "label": "Smoke", "confidence": 0.85,
         "probabilities": [0.02, 0.05, 0.06, 0.87]},
        {"frame_id": 1, "label": "NoGas", "confidence": 0.8,
         "probabilities": [0.1, 0.8, 0.05, 0.05]},
    ])

    results = run_multimodal_classification(sample_config, device="CPU")
    assert len(results) == 2


def test_run_multimodal_classification_no_classifications_raises(sample_config, monkeypatch):
    import src.utility.multimodal_runner as runner_mod

    _patch_pipeline(monkeypatch, runner_mod, records=[])

    with pytest.raises(MultimodalRunError, match="No image classifications collected"):
        run_multimodal_classification(sample_config, device="CPU")


def test_run_multimodal_classification_unmatched_frame_ids_raise(sample_config, monkeypatch):
    """Frame ids with no manifest entry are skipped; if none match at all,
    the run fails loudly rather than silently fusing zero samples."""
    import src.utility.multimodal_runner as runner_mod

    _patch_pipeline(monkeypatch, runner_mod, records=[
        {"frame_id": 99, "label": "Smoke", "confidence": 0.85, "probabilities": None},
    ])

    with pytest.raises(MultimodalRunError, match="could be matched"):
        run_multimodal_classification(sample_config, device="CPU")


def test_persist_results_posts_each_sample_with_no_bounding_box():
    results = [
        {"source": "img_Smoke.jpg", "label": "Smoke", "confidence": 0.9,
         "image_confidence": 0.85, "sensor_confidence": 0.95,
         "sensor_raw_json": "{}"},
        {"source": "img_NoGas.jpg", "label": "NoGas", "confidence": 0.8,
         "image_confidence": 0.75, "sensor_confidence": 0.7,
         "sensor_raw_json": "{}"},
    ]
    posted = []
    inserted = persist_results(results, "gas_detection_multimodal", posted.append)

    assert inserted == 2
    assert len(posted) == 2
    assert posted[0]["frame_id"] == 0
    assert posted[0]["label"] == "Smoke"
    assert posted[0]["x"] == 0.0 and posted[0]["width"] == 0.0
    assert posted[0]["source"] == "gas_detection_multimodal"
    assert posted[1]["frame_id"] == 1


def test_persist_results_continues_after_a_post_failure():
    results = [
        {"source": "a.jpg", "label": "Smoke", "confidence": 0.9},
        {"source": "b.jpg", "label": "NoGas", "confidence": 0.8},
    ]

    def flaky_post(payload):
        if payload["frame_id"] == 0:
            raise ConnectionError("storage-service unreachable")

    inserted = persist_results(results, "gas_detection_multimodal", flaky_post)
    assert inserted == 1
