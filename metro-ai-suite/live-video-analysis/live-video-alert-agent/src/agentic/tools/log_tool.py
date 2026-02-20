"""
log_alert tool — records an alert event to the application log and the
in-memory history managed by AlertStateManager.

This is always the baseline tool included in every alert's tool list.
It does not require any external service configuration.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def log_alert(
    stream_id: str,
    alert_name: str,
    severity: str,
    answer: str,
    reason: str,
    consecutive_count: int = 1,
    escalated: bool = False,
    snapshot_path: Optional[str] = None,
) -> dict:
    """
    Log an alert detection event.

    This tool is always executed for every YES detection regardless of
    cooldown (recording is separate from action suppression handled by
    AlertStateManager).

    Parameters
    ----------
    stream_id:        Source stream identifier.
    alert_name:       Alert configuration name.
    severity:         Severity string (low / medium / high / critical).
    answer:           "YES" or "NO".
    reason:           VLM explanation.
    consecutive_count: Consecutive YES detections at time of logging.
    escalated:        Whether this event triggered escalation.
    snapshot_path:    Optional path to saved snapshot file.
    """
    level = logging.WARNING if answer == "YES" else logging.DEBUG
    logger.log(
        level,
        f"[{severity.upper()}] ALERT {answer} | stream={stream_id} | "
        f"alert={alert_name} | consecutive={consecutive_count} | "
        f"escalated={escalated} | reason={reason!r}"
        + (f" | snapshot={snapshot_path}" if snapshot_path else ""),
    )
    return {
        "status": "logged",
        "stream_id": stream_id,
        "alert_name": alert_name,
        "severity": severity,
        "answer": answer,
        "consecutive_count": consecutive_count,
        "escalated": escalated,
    }
