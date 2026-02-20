"""
publish_mqtt tool — publishes alert notifications to an MQTT broker.

Configuration (environment variables):
    MQTT_BROKER      — hostname/IP of the MQTT broker
    MQTT_PORT        — broker port (default 1883)
    MQTT_USERNAME    — optional username
    MQTT_PASSWORD    — optional password
    MQTT_BASE_TOPIC  — base topic prefix (default: live-video-alerts)

Published topic:   {MQTT_BASE_TOPIC}/{stream_id}/{alert_name}
Payload format (JSON):
    {
        "stream_id":  "cam1",
        "alert_name": "Fire Detection",
        "severity":   "critical",
        "answer":     "YES",
        "reason":     "Flames visible in upper right corner",
        "timestamp":  "2026-02-20T14:35:00Z"
    }

Requires: paho-mqtt>=2.0.0
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


async def publish_mqtt(
    stream_id: str,
    alert_name: str,
    severity: str,
    answer: str,
    reason: str,
    topic_override: Optional[str] = None,
) -> dict:
    """
    Publish an alert event to an MQTT broker.

    Parameters
    ----------
    stream_id:      Source stream identifier.
    alert_name:     Alert configuration name.
    severity:       Alert severity string.
    answer:         "YES" or "NO".
    reason:         VLM explanation.
    topic_override: Use this topic instead of the default pattern.
    """
    from src.config import settings

    broker = settings.MQTT_BROKER
    if not broker:
        logger.warning("publish_mqtt: MQTT_BROKER not configured — skipping")
        return {"status": "skipped", "reason": "MQTT_BROKER not configured"}

    topic = topic_override or (
        f"{settings.MQTT_BASE_TOPIC}/"
        f"{stream_id.replace(' ', '_')}/"
        f"{alert_name.replace(' ', '_')}"
    )

    payload = json.dumps({
        "stream_id": stream_id,
        "alert_name": alert_name,
        "severity": severity,
        "answer": answer,
        "reason": reason,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    })

    def _publish():
        import paho.mqtt.client as mqtt

        client = mqtt.Client(
            client_id=f"live-video-alert-{int(time.time())}",
            protocol=mqtt.MQTTv5,
        )
        if settings.MQTT_USERNAME:
            client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

        client.connect(broker, settings.MQTT_PORT, keepalive=10)
        result = client.publish(topic, payload, qos=1)
        result.wait_for_publish(timeout=5)
        client.disconnect()
        return result.rc  # 0 = MQTT_ERR_SUCCESS

    try:
        rc = await asyncio.to_thread(_publish)
        if rc == 0:
            logger.info(f"MQTT published | topic={topic} | alert={alert_name}")
            return {"status": "published", "topic": topic, "rc": rc}
        else:
            logger.error(f"MQTT publish failed | rc={rc}")
            return {"status": "error", "topic": topic, "rc": rc}
    except Exception as exc:
        logger.error(f"publish_mqtt error: {exc}")
        return {"status": "error", "reason": str(exc)}
