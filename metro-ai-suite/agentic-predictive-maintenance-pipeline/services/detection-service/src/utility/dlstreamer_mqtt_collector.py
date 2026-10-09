# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral MQTT collector for a single bounded DL Streamer classification run.

Used by the gas-detection image branch: unlike the persistent
``mqtt_subscriber.py`` (which writes every raw detection straight to
storage-service), per-frame *image-only* classifications from the
``gvaclassify`` pipeline are not meant to be durably persisted on their own —
they only become a real result once fused with the paired sensor reading (see
``multimodal_runner.py``). So this collector subscribes to a **dedicated**
topic (not the shared ``apm/detections`` topic ``mqtt_subscriber.py`` owns)
only for the lifetime of one run, buffers messages in memory, and is torn
down once the run completes — nothing here ever touches storage-service.
"""

import json
import logging
import os
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

_MQTT_HOST  = os.environ.get("MQTT_HOST", "mqtt-broker")
_MQTT_PORT  = int(os.environ.get("MQTT_PORT", "1883"))
_DEFAULT_TOPIC = os.environ.get("GAS_CLASSIFICATION_MQTT_TOPIC", "apm/gas-image-classifications")

# Frame rate of the source video, used to convert DL Streamer's per-frame
# timestamp (ns) back to a frame index. Must match the fps the video was built
# with (scripts/download_and_prep_data.py writes it at 30 fps).
_DEFAULT_FPS = 30.0

_SUBSCRIBE_TIMEOUT_S = 10.0
# After the pipeline reports COMPLETED, wait this long for trailing in-flight
# messages before giving up on reaching the expected frame count.
_DRAIN_TIMEOUT_S = 5.0
_DRAIN_POLL_S = 0.1


class CollectorError(Exception):
    """Raised when the collector cannot connect/subscribe to the MQTT broker."""


def _timestamp_to_frame_id(timestamp_ns: int, fps: float) -> int:
    # round() rather than floor so PTS values a few ns below the exact frame
    # boundary (encoder timebase rounding) still map to the intended frame.
    return int(round(timestamp_ns * fps / 1_000_000_000))


def _extract_classification(payload: dict, fps: float = _DEFAULT_FPS) -> dict | None:
    """Parse one DL Streamer gvaclassify MQTT message into a flat record.

    DL Streamer wraps whole-frame classification results as:
    ``{"metadata": {"objects": [{"tensors": [{"label": ..., "confidence": ...,
    "data": [...]}], "x":0, "y":0, "w":<frame_w>, "h":<frame_h>}], "timestamp": ...}}``
    (a "full-frame" ROI object carrying one or more classifier tensors, rather
    than gvadetect's ``detection`` dict). Falls back gracefully across minor
    schema variations since the exact shape should be confirmed against a real
    running pipeline.
    """
    if not isinstance(payload, dict) or "metadata" not in payload:
        return None
    meta = payload["metadata"]
    timestamp_ns = meta.get("timestamp", 0)
    frame_id = _timestamp_to_frame_id(timestamp_ns, fps)

    objects = meta.get("objects", [])
    if not objects:
        return None

    # Whole-frame classification: take the first object's first tensor.
    obj = objects[0]
    tensors = obj.get("tensors", [])
    label = None
    confidence = None
    probabilities = None
    for tensor in tensors:
        if "label" in tensor or "confidence" in tensor:
            label = tensor.get("label")
            confidence = tensor.get("confidence")
            # Raw per-class probability vector, if the DL Streamer version
            # emits it (e.g. via gvametaconvert's tensor-data passthrough).
            probabilities = tensor.get("data")
            break

    if label is None:
        label = obj.get("roi_type")

    return {
        "frame_id": int(frame_id),
        "video_time_seconds": float(timestamp_ns) / 1_000_000_000,
        "label": label or "unknown",
        "confidence": float(confidence) if confidence is not None else 0.0,
        "probabilities": list(probabilities) if probabilities else None,
    }


class GasClassificationCollector:
    """Collects one bounded run's worth of whole-frame classification results."""

    def __init__(self, topic: str | None = None, fps: float = _DEFAULT_FPS):
        self.topic = topic or _DEFAULT_TOPIC
        self.fps = fps
        self._results: list[dict] = []
        self._lock = threading.Lock()
        self._subscribed = threading.Event()
        self._client: mqtt.Client | None = None

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(self.topic)
            log.info("Gas classification collector connected; subscribing to %s", self.topic)
        else:
            log.error("Gas classification collector MQTT connection failed with rc=%s", rc)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        if any(getattr(rc, "is_failure", False) for rc in reason_codes or []):
            log.error("Gas classification collector subscribe to %s rejected: %s",
                      self.topic, reason_codes)
            return
        self._subscribed.set()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            record = _extract_classification(payload, fps=self.fps)
            if record is not None:
                with self._lock:
                    self._results.append(record)
        except Exception as exc:
            log.error("Error processing gas classification MQTT message: %s", exc)

    def start(self, subscribe_timeout: float = _SUBSCRIBE_TIMEOUT_S) -> None:
        """Connect and block until the subscription is acknowledged by the
        broker, so no frames published right after the pipeline starts are
        missed. Raises ``CollectorError`` on timeout."""
        self._subscribed.clear()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message
        try:
            self._client.connect(_MQTT_HOST, _MQTT_PORT, keepalive=60)
        except OSError as exc:
            self._client = None
            raise CollectorError(
                f"Cannot connect to MQTT broker {_MQTT_HOST}:{_MQTT_PORT}: {exc}"
            ) from exc
        self._client.loop_start()
        if not self._subscribed.wait(subscribe_timeout):
            self._disconnect()
            raise CollectorError(
                f"Timed out after {subscribe_timeout}s subscribing to MQTT topic {self.topic}"
            )

    def _count(self) -> int:
        with self._lock:
            return len({r["frame_id"] for r in self._results})

    def _disconnect(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def stop(self, expected_count: int | None = None,
             drain_timeout: float = _DRAIN_TIMEOUT_S) -> list[dict]:
        """Disconnect and return all classification records collected so far,
        sorted by ``frame_id``.

        If ``expected_count`` is given, first waits up to ``drain_timeout``
        seconds for that many distinct frames to arrive, since QoS-0 messages
        published just before the pipeline reports COMPLETED may still be in
        flight.
        """
        if self._client is not None and expected_count:
            deadline = time.monotonic() + drain_timeout
            while self._count() < expected_count and time.monotonic() < deadline:
                time.sleep(_DRAIN_POLL_S)
            received = self._count()
            if received < expected_count:
                log.warning("Gas classification collector received %d/%d frames on %s",
                            received, expected_count, self.topic)
        self._disconnect()
        with self._lock:
            results = sorted(self._results, key=lambda r: r["frame_id"])
        return results
