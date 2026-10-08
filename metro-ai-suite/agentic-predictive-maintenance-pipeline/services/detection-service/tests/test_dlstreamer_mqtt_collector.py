# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Tests for dlstreamer_mqtt_collector.py's MQTT payload parsing and the
in-memory collection/sort behavior (no real MQTT broker involved)."""

from src.utility.dlstreamer_mqtt_collector import (
    GasClassificationCollector,
    _extract_classification,
)


def test_extract_classification_reads_label_confidence_and_probabilities():
    payload = {
        "metadata": {
            "timestamp": 3_333_333_330,  # ~frame 100 at 30fps
            "objects": [
                {"x": 0, "y": 0, "w": 640, "h": 640, "roi_type": "",
                 "tensors": [{"label": "Smoke", "confidence": 0.87,
                              "data": [0.02, 0.05, 0.06, 0.87]}]}
            ],
        }
    }
    record = _extract_classification(payload)
    assert record == {
        "frame_id": 100, "video_time_seconds": 3.33333333,
        "label": "Smoke", "confidence": 0.87,
        "probabilities": [0.02, 0.05, 0.06, 0.87],
    }


def test_extract_classification_falls_back_to_roi_type_label():
    payload = {"metadata": {"timestamp": 0, "objects": [{"roi_type": "NoGas"}]}}
    record = _extract_classification(payload)
    assert record["label"] == "NoGas"
    assert record["confidence"] == 0.0
    assert record["probabilities"] is None


def test_extract_classification_returns_none_for_unrecognized_payload():
    assert _extract_classification({"foo": "bar"}) is None
    assert _extract_classification({"metadata": {"objects": []}}) is None


def test_collector_on_message_buffers_and_stop_sorts_by_frame_id():
    collector = GasClassificationCollector(topic="apm/test-topic")

    def make_msg(frame_id):
        class Msg:
            payload = (
                b'{"metadata": {"timestamp": %d, "objects": '
                b'[{"tensors": [{"label": "Smoke", "confidence": 0.9}]}]}}'
                % (frame_id * 33_333_333)
            )
        return Msg()

    collector._on_message(None, None, make_msg(2))
    collector._on_message(None, None, make_msg(0))
    collector._on_message(None, None, make_msg(1))

    results = collector.stop()
    assert [r["frame_id"] for r in results] == [0, 1, 2]


def test_collector_on_message_ignores_malformed_payload():
    collector = GasClassificationCollector(topic="apm/test-topic")

    class Msg:
        payload = b"not json"

    collector._on_message(None, None, Msg())
    assert collector.stop() == []


def test_extract_classification_uses_configured_fps():
    # Frame 10 of a 10fps video is at t=1s.
    payload = {"metadata": {"timestamp": 1_000_000_000,
                            "objects": [{"tensors": [{"label": "Smoke", "confidence": 0.9}]}]}}
    assert _extract_classification(payload, fps=10)["frame_id"] == 10
    assert _extract_classification(payload, fps=30)["frame_id"] == 30


def test_extract_classification_rounds_timestamps_just_below_frame_boundary():
    payload = {"metadata": {"timestamp": 33_333_332,
                            "objects": [{"tensors": [{"label": "Smoke", "confidence": 0.9}]}]}}
    assert _extract_classification(payload)["frame_id"] == 1


class _FakeMqttClient:
    """Minimal paho client stand-in: acknowledges the subscription on connect."""

    def __init__(self, *_args, ack=True, **_kwargs):
        self.ack = ack
        self.on_connect = self.on_subscribe = self.on_message = None
        self.stopped = False

    def connect(self, *_args, **_kwargs):
        pass

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def subscribe(self, _topic):
        if self.ack:
            self.on_subscribe(self, None, 1, [])

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        pass


def test_start_waits_for_subscription_ack(monkeypatch):
    import src.utility.dlstreamer_mqtt_collector as mod

    monkeypatch.setattr(mod.mqtt, "Client", lambda *a, **k: _FakeMqttClient())
    collector = GasClassificationCollector(topic="apm/test-topic")
    collector.start()
    assert collector._subscribed.is_set()
    collector.stop()


def test_start_raises_when_subscription_not_acknowledged(monkeypatch):
    import pytest
    import src.utility.dlstreamer_mqtt_collector as mod

    monkeypatch.setattr(mod.mqtt, "Client", lambda *a, **k: _FakeMqttClient(ack=False))
    collector = GasClassificationCollector(topic="apm/test-topic")
    with pytest.raises(mod.CollectorError):
        collector.start(subscribe_timeout=0.05)
    assert collector._client is None


def test_stop_drains_until_expected_frames_arrive(monkeypatch):
    import threading
    import src.utility.dlstreamer_mqtt_collector as mod

    monkeypatch.setattr(mod.mqtt, "Client", lambda *a, **k: _FakeMqttClient())
    collector = GasClassificationCollector(topic="apm/test-topic")
    collector.start()

    class Msg:
        payload = (b'{"metadata": {"timestamp": %d, "objects": '
                   b'[{"tensors": [{"label": "Smoke", "confidence": 0.9}]}]}}' % 33_333_333)

    # A trailing frame that arrives shortly after stop() is called.
    threading.Timer(0.2, collector._on_message, args=(None, None, Msg())).start()
    results = collector.stop(expected_count=1, drain_timeout=2.0)
    assert [r["frame_id"] for r in results] == [1]
