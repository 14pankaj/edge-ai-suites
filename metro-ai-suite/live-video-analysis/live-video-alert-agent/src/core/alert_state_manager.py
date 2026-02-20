"""
AlertStateManager — per-stream, per-alert state tracking.

Responsibilities
----------------
- Deduplication: suppress actions within a configurable cooldown window.
- Transition detection: detect YES→NO and NO→YES state changes.
- Escalation: count consecutive YES detections and signal escalation when
  the configured threshold is reached.
- History: maintain a fixed-size ring buffer of AlertEvent records.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.config import settings
from src.schemas.monitor import (
    AlertConfig,
    AlertEvent,
    AlertRuntimeState,
    AlertSeverity,
)

logger = logging.getLogger(__name__)


class AlertStateManager:
    """
    Tracks runtime alert state for all streams and all alerts.

    State dictionary layout::

        _state[stream_id][alert_name] = AlertRuntimeState(...)

    History is a single global deque (newest last).
    """

    def __init__(self, history_size: int = 0):
        self._state: Dict[str, Dict[str, AlertRuntimeState]] = {}
        self._history: deque[AlertEvent] = deque(
            maxlen=history_size or settings.ALERT_HISTORY_SIZE
        )

    # ------------------------------------------------------------------ #
    # Stream lifecycle
    # ------------------------------------------------------------------ #

    def register_stream(self, stream_id: str):
        if stream_id not in self._state:
            self._state[stream_id] = {}

    def unregister_stream(self, stream_id: str):
        self._state.pop(stream_id, None)

    # ------------------------------------------------------------------ #
    # Core processing
    # ------------------------------------------------------------------ #

    def process(
        self,
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,        # "YES" or "NO"
        reason: str,
    ) -> Tuple[bool, bool, bool]:
        """
        Update state for one (stream, alert) pair and determine what actions
        should be taken.

        Returns
        -------
        (should_act, is_escalation, is_transition)
            should_act   : True if tools should be invoked (cooldown passed)
            is_escalation: True if the escalation threshold was just reached
            is_transition: True if the answer changed from the previous cycle
                           (useful for dashboard "state change" events)
        """
        if stream_id not in self._state:
            self._state[stream_id] = {}

        state = self._state[stream_id].get(alert_cfg.name)
        if state is None:
            state = AlertRuntimeState()
            self._state[stream_id][alert_cfg.name] = state

        now = time.monotonic()

        # --- transition detection ---
        is_transition = answer != state.last_answer
        if is_transition:
            state.last_transition_ts = now

        # --- consecutive YES counter ---
        if answer == "YES":
            state.consecutive_yes += 1
        else:
            state.consecutive_yes = 0

        state.last_answer = answer  # type: ignore[assignment]

        if answer == "NO":
            return False, False, is_transition

        # --- cooldown check ---
        cooldown_passed = (
            state.last_action_ts is None
            or (now - state.last_action_ts) >= alert_cfg.cooldown_seconds
        )

        should_act = cooldown_passed

        # --- escalation check ---
        is_escalation = False
        if (
            should_act
            and alert_cfg.escalation
            and state.consecutive_yes >= alert_cfg.escalation.threshold_consecutive
        ):
            is_escalation = True
            logger.warning(
                f"ESCALATION [{stream_id}][{alert_cfg.name}] "
                f"— {state.consecutive_yes} consecutive detections"
            )

        if should_act:
            state.last_action_ts = now

        return should_act, is_escalation, is_transition

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #

    def record_event(
        self,
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,
        reason: str,
        actions_taken: List[str],
        escalated: bool,
        snapshot_path: Optional[str] = None,
    ) -> AlertEvent:
        """Append an AlertEvent to the history ring buffer and return it."""
        state = self._state.get(stream_id, {}).get(alert_cfg.name)
        consecutive = state.consecutive_yes if state else 1

        event = AlertEvent(
            event_id=str(uuid.uuid4()),
            timestamp=datetime.now(tz=timezone.utc),
            stream_id=stream_id,
            alert_name=alert_cfg.name,
            severity=alert_cfg.severity,
            answer=answer,          # type: ignore[arg-type]
            reason=reason,
            consecutive_count=consecutive,
            actions_taken=actions_taken,
            escalated=escalated,
            snapshot_path=snapshot_path,
        )
        self._history.append(event)
        return event

    def get_history(
        self,
        stream_id: Optional[str] = None,
        alert_name: Optional[str] = None,
        severity: Optional[AlertSeverity] = None,
        answer: Optional[str] = None,
        limit: int = 50,
    ) -> List[AlertEvent]:
        """Return matching history entries newest-first."""
        results = list(self._history)
        results.reverse()  # newest first

        if stream_id:
            results = [e for e in results if e.stream_id == stream_id]
        if alert_name:
            results = [e for e in results if e.alert_name == alert_name]
        if severity:
            results = [e for e in results if e.severity == severity]
        if answer:
            results = [e for e in results if e.answer == answer]

        return results[:limit]

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def get_consecutive_count(self, stream_id: str, alert_name: str) -> int:
        return self._state.get(stream_id, {}).get(alert_name, AlertRuntimeState()).consecutive_yes

    def get_last_answer(self, stream_id: str, alert_name: str) -> str:
        return self._state.get(stream_id, {}).get(alert_name, AlertRuntimeState()).last_answer
