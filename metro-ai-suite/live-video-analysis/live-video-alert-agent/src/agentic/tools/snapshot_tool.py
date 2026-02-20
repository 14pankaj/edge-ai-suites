"""
capture_snapshot tool — writes the latest frame from a stream to disk.

Configuration (environment variables):
    SNAPSHOT_DIR — base directory for snapshot files (default: ``snapshots/``)

File naming:  {SNAPSHOT_DIR}/{stream_id}/{alert_name}_{timestamp}.jpg
"""

import asyncio
import logging
import os
import time
from typing import Optional

import cv2

logger = logging.getLogger(__name__)

# Registry: stream_id → frame retrieval callback (injected by AgentManager)
# Signature: (stream_id: str) -> Optional[np.ndarray]
_frame_callbacks: dict = {}


def register_frame_callback(stream_id: str, callback):
    """Called by AgentManager to register per-stream frame accessors."""
    _frame_callbacks[stream_id] = callback


def unregister_frame_callback(stream_id: str):
    _frame_callbacks.pop(stream_id, None)


async def capture_snapshot(
    stream_id: str,
    alert_name: str = "alert",
    severity: str = "medium",
) -> dict:
    """
    Save the current frame from *stream_id* as a JPEG snapshot.

    Parameters
    ----------
    stream_id:   Source stream identifier.
    alert_name:  Included in the filename for easy filtering.
    severity:    Included in the filename for easy filtering.
    """
    from src.config import settings

    callback = _frame_callbacks.get(stream_id)
    if callback is None:
        logger.warning(f"capture_snapshot: no frame callback for stream '{stream_id}'")
        return {"status": "skipped", "reason": "no frame callback registered"}

    # Retrieve frame (callback may be sync — run in thread pool if needed)
    try:
        frame = callback(stream_id)
    except Exception as exc:
        logger.error(f"capture_snapshot: frame callback error: {exc}")
        return {"status": "error", "reason": str(exc)}

    if frame is None:
        return {"status": "skipped", "reason": "no frame available"}

    # Build output path
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_alert = alert_name.replace(" ", "_").replace("/", "_")
    safe_stream = stream_id.replace("/", "_").replace(":", "_")
    out_dir = os.path.join(settings.SNAPSHOT_DIR, safe_stream)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"{safe_alert}_{severity}_{ts}.jpg"
    path = os.path.join(out_dir, filename)

    # Write to disk (blocking I/O in thread pool)
    def _write():
        cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

    await asyncio.to_thread(_write)

    logger.info(f"Snapshot saved: {path}")
    return {"status": "saved", "path": path}
