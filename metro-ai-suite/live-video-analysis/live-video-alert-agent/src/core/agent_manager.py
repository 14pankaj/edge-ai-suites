# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
AgentManager — central orchestrator for multi-camera alert processing.

Key improvements
----------------
- Concurrent stream analysis: all streams are analysed in parallel via
  asyncio.gather(), so cycle time = max(VLM latency) not sum(VLM latency).
- Per-stream independent analysis loops 
- AlertStateManager integration: deduplication, cooldown, escalation.
- AlertActionAgent integration: Google ADK tool-calling (or rule-based fallback).
- Snapshot tool callback registration per stream.
- Proper AlertConfig (Pydantic) instead of raw dicts for agent configuration.
- Stored alert history accessible via API.
- Runtime metrics: per-stream analysis counters and inference latency.
- Graceful shutdown: cancels all per-stream tasks cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from .stream_manager import LiveStreamManager
from .vlm_client import VLMClient
from .event_manager import EventManager
from .alert_state_manager import AlertStateManager
from src.agentic.alert_agent import AlertActionAgent
from src.agentic.tools.snapshot_tool import register_frame_callback, unregister_frame_callback
from src.schemas.monitor import AgentResult, AlertConfig, AlertSeverity
from src.config import settings

logger = logging.getLogger(__name__)

_RESOURCES = Path("resources")


class StreamMetrics:
    """Runtime counters for a single stream."""
    __slots__ = ("analysis_count", "alert_count", "last_inference_ms")

    def __init__(self):
        self.analysis_count: int = 0
        self.alert_count: int = 0
        self.last_inference_ms: Optional[float] = None


class AgentManager:
    """
    Manages all camera streams, VLM inference, alert state, and action dispatch.

    One AgentManager instance handles N camera streams concurrently.
    Each stream gets its own asyncio Task running an independent analysis loop.
    """

    def __init__(
        self,
        vlm_url: str,
        vlm_api_key: str,
        model_name: str,
        streams_config: str = str(_RESOURCES / "streams.json"),
        agents_config: str = str(_RESOURCES / "agents.json"),
    ):
        self._streams_config_file = streams_config
        self._agents_config_file = agents_config

        # Camera streams: stream_id → LiveStreamManager
        self.streams: Dict[str, LiveStreamManager] = {}

        # VLM client (shared across all stream loops)
        self.vlm_client = VLMClient(
            base_url=vlm_url,
            api_key=vlm_api_key,
            model_name=model_name,
        )

        # SSE pub/sub
        self.events = EventManager()

        # Alert state (dedup, cooldown, escalation, history)
        self.alert_state = AlertStateManager()

        # Action agent (ADK or rule-based)
        self.action_agent = AlertActionAgent()

        # Latest analysis results, exposed by /data legacy endpoint
        # Structure: { stream_id: { alert_name: {"answer": ..., "reason": ...} } }
        self.latest_results: Dict[str, Dict] = {}

        # Runtime metrics per stream
        self._metrics: Dict[str, StreamMetrics] = {}

        # Per-stream analysis asyncio Tasks
        self._stream_tasks: Dict[str, asyncio.Task] = {}

        self.running = False
        self._start_time: Optional[float] = None

        # Load persistent configs
        self.alerts: List[AlertConfig] = self._load_alerts_config()
        self._load_streams_config()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self):
        """Start all registered stream managers and their analysis loops."""
        self.running = True
        self._start_time = time.monotonic()

        for stream_id, mgr in self.streams.items():
            mgr.start()
            self._launch_stream_task(stream_id)

        logger.info(
            f"AgentManager started — {len(self.streams)} stream(s), "
            f"{len(self.alerts)} alert(s), "
            f"ADK={'on' if settings.USE_ADK else 'off'}"
        )

        # Keep alive until stop() is called
        while self.running:
            await asyncio.sleep(5)

    def stop(self):
        """Signal all loops to stop and cancel their tasks."""
        self.running = False
        for task in self._stream_tasks.values():
            task.cancel()
        for mgr in self.streams.values():
            mgr.stop()
        logger.info("AgentManager stopped")

    @property
    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    # ------------------------------------------------------------------ #
    # Stream management
    # ------------------------------------------------------------------ #

    def add_stream(self, stream_id: str, rtsp_url: str, save: bool = True):
        if stream_id in self.streams:
            logger.warning(f"Stream '{stream_id}' already registered — ignoring")
            return

        mgr = LiveStreamManager(rtsp_url)
        self.streams[stream_id] = mgr
        self.latest_results[stream_id] = {}
        self._metrics[stream_id] = StreamMetrics()
        self.alert_state.register_stream(stream_id)

        # Register snapshot callback
        register_frame_callback(
            stream_id,
            lambda sid: self._get_latest_frame(sid),
        )

        if self.running:
            mgr.start()
            self._launch_stream_task(stream_id)

        if save:
            self._save_streams_config()

        logger.info(f"Stream added: {stream_id} → {rtsp_url}")

    def remove_stream(self, stream_id: str):
        if stream_id not in self.streams:
            return

        # Cancel analysis task
        task = self._stream_tasks.pop(stream_id, None)
        if task:
            task.cancel()

        self.streams[stream_id].stop()
        del self.streams[stream_id]
        self.latest_results.pop(stream_id, None)
        self._metrics.pop(stream_id, None)
        self.alert_state.unregister_stream(stream_id)
        unregister_frame_callback(stream_id)
        self._save_streams_config()
        logger.info(f"Stream removed: {stream_id}")

    def get_latest_frame(self, stream_id: str):
        return self._get_latest_frame(stream_id)

    def _get_latest_frame(self, stream_id: str):
        mgr = self.streams.get(stream_id)
        if mgr is None:
            return None
        frames = mgr.get_recent_frames(count=1)
        return frames[0] if frames else None

    # ------------------------------------------------------------------ #
    # Alert / agent configuration
    # ------------------------------------------------------------------ #

    def get_alerts_config(self) -> List[dict]:
        return [a.model_dump() for a in self.alerts]

    def save_alerts_config(self, config_data: List[dict]) -> None:
        """Validate, apply, and persist new alert configurations."""
        new_alerts: List[AlertConfig] = []
        for entry in config_data:
            try:
                new_alerts.append(AlertConfig(**entry))
            except ValidationError as exc:
                raise ValueError(f"Invalid alert config: {exc}") from exc

        self.alerts = new_alerts
        try:
            _RESOURCES.mkdir(parents=True, exist_ok=True)
            with open(self._agents_config_file, "w") as fh:
                json.dump([a.model_dump() for a in self.alerts], fh, indent=2)
            logger.info(f"Saved {len(self.alerts)} alert config(s)")
        except Exception as exc:
            logger.error(f"Failed to persist alert config: {exc}")

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #

    def get_stream_metrics(self) -> List[dict]:
        results = []
        for sid, m in self._metrics.items():
            results.append({
                "stream_id": sid,
                "analysis_count": m.analysis_count,
                "alert_count": m.alert_count,
                "last_inference_ms": m.last_inference_ms,
            })
        return results

    # ------------------------------------------------------------------ #
    # SSE subscription
    # ------------------------------------------------------------------ #

    async def subscribe(self) -> asyncio.Queue:
        return await self.events.subscribe()

    async def unsubscribe(self, queue: asyncio.Queue):
        await self.events.unsubscribe(queue)

    # ------------------------------------------------------------------ #
    # Per-stream analysis task
    # ------------------------------------------------------------------ #

    def _launch_stream_task(self, stream_id: str):
        """Create and track an independent asyncio Task for one stream."""
        if stream_id in self._stream_tasks:
            existing = self._stream_tasks[stream_id]
            if not existing.done():
                return  # already running

        task = asyncio.create_task(
            self._stream_analysis_loop(stream_id),
            name=f"analysis-{stream_id}",
        )
        task.add_done_callback(
            lambda t: self._on_task_done(stream_id, t)
        )
        self._stream_tasks[stream_id] = task

    def _on_task_done(self, stream_id: str, task: asyncio.Task):
        """Restart crashed analysis tasks while the manager is still running."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Analysis task for '{stream_id}' crashed: {exc}")
            if self.running and stream_id in self.streams:
                logger.info(f"Restarting analysis task for '{stream_id}'")
                self._launch_stream_task(stream_id)

    async def _stream_analysis_loop(self, stream_id: str):
        """
        Independent analysis loop for a single stream.

        Runs at ANALYSIS_INTERVAL cadence.  Because each stream has its own
        task, streams are analysed concurrently — one slow VLM call does not
        delay other streams.
        """
        logger.info(f"Analysis loop started for stream '{stream_id}'")

        # Let the stream buffer pre-fill
        await asyncio.sleep(2)

        while self.running and stream_id in self.streams:
            t_start = time.monotonic()
            await self._analyse_one_stream(stream_id)

            elapsed = time.monotonic() - t_start
            sleep_time = max(0.1, settings.ANALYSIS_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

        logger.info(f"Analysis loop exited for stream '{stream_id}'")

    # ------------------------------------------------------------------ #
    # Core analysis for one stream
    # ------------------------------------------------------------------ #

    async def _analyse_one_stream(self, stream_id: str):
        """Run one VLM inference cycle for a single stream."""
        mgr = self.streams.get(stream_id)
        if not mgr:
            return

        frames = mgr.get_recent_frames(count=1)
        if not frames:
            return

        enabled = [a for a in self.alerts if a.enabled]
        if not enabled:
            return

        prompt = self._build_vlm_prompt(enabled)

        logger.debug(
            f"[{stream_id}] Inference cycle — frames={len(frames)} "
            f"alerts={[a.name for a in enabled]}"
        )
        response = await self.vlm_client.analyze_stream_segment(
            frames,
            system_prompt=(
                "You are a precise video analytics AI. "
                "Always respond with valid JSON only, no markdown."
            ),
            user_prompt=prompt,
        )

        metrics = self._metrics.get(stream_id)
        if metrics:
            metrics.analysis_count += 1
            metrics.last_inference_ms = self.vlm_client.last_inference_ms

        if not response:
            return

        logger.debug(f"[{stream_id}] VLM response: {response[:300]!r}")
        parsed = self._parse_vlm_response(response, enabled)
        if not parsed:
            return

        self.latest_results[stream_id] = parsed

        # Broadcast raw results for the dashboard
        await self.events.broadcast("analysis", {
            "stream_id": stream_id,
            "results": parsed,
        })

        # Process each alert through state manager + action agent
        await self._process_alerts(stream_id, enabled, parsed)

    # ------------------------------------------------------------------ #
    # Alert processing (state + actions)
    # ------------------------------------------------------------------ #

    async def _process_alerts(
        self,
        stream_id: str,
        enabled: List[AlertConfig],
        parsed: dict,
    ):
        """
        For each triggered alert: check cooldown/escalation, then dispatch
        tools via the action agent.
        """
        for alert_cfg in enabled:
            result = parsed.get(alert_cfg.name)
            if not result:
                continue

            answer = result.get("answer", "NO")
            reason = result.get("reason", "")

            should_act, is_escalation, is_transition = self.alert_state.process(
                stream_id=stream_id,
                alert_cfg=alert_cfg,
                answer=answer,
                reason=reason,
            )

            if answer == "YES":
                metrics = self._metrics.get(stream_id)
                if metrics and is_transition:
                    metrics.alert_count += 1

                logger.warning(
                    f"ALERT YES | stream={stream_id} | alert={alert_cfg.name} | "
                    f"severity={alert_cfg.severity.value} | "
                    f"act={should_act} | escalated={is_escalation}"
                )

            if not should_act:
                continue

            # Capture snapshot first (so path is available to other tools like email).
            # Done here — NOT re-run inside dispatch — to avoid double-writing.
            snapshot_path: Optional[str] = None
            wants_snapshot = "capture_snapshot" in alert_cfg.tools or (
                is_escalation
                and alert_cfg.escalation
                and "capture_snapshot" in alert_cfg.escalation.additional_tools
            )
            if wants_snapshot:
                try:
                    from src.agentic.tools.snapshot_tool import capture_snapshot
                    snap_result = await capture_snapshot(
                        stream_id=stream_id,
                        alert_name=alert_cfg.name,
                        severity=alert_cfg.severity.value,
                    )
                    snapshot_path = snap_result.get("path")
                except Exception as snap_exc:
                    logger.error(f"Snapshot capture failed for '{stream_id}': {snap_exc}")

            # Build a tool list that excludes capture_snapshot — it was already
            # invoked above, so passing it to dispatch would run it a second time.
            import copy
            dispatch_cfg = copy.copy(alert_cfg)
            dispatch_cfg.tools = [t for t in alert_cfg.tools if t != "capture_snapshot"]

            # Dispatch via action agent
            actions_taken = await self.action_agent.dispatch(
                stream_id=stream_id,
                alert_cfg=dispatch_cfg,
                answer=answer,
                reason=reason,
                consecutive_count=self.alert_state.get_consecutive_count(
                    stream_id, alert_cfg.name
                ),
                escalated=is_escalation,
                snapshot_path=snapshot_path,
            )

            # Re-add capture_snapshot to actions_taken if it succeeded
            if snapshot_path and "capture_snapshot" not in actions_taken:
                actions_taken = ["capture_snapshot"] + actions_taken

            # Record to history
            self.alert_state.record_event(
                stream_id=stream_id,
                alert_cfg=alert_cfg,
                answer=answer,
                reason=reason,
                actions_taken=actions_taken,
                escalated=is_escalation,
                snapshot_path=snapshot_path,
            )

            # Broadcast enriched event to dashboard
            if answer == "YES":
                await self.events.broadcast("alert_action", {
                    "stream_id": stream_id,
                    "alert_name": alert_cfg.name,
                    "severity": alert_cfg.severity.value,
                    "answer": answer,
                    "reason": reason,
                    "actions_taken": actions_taken,
                    "escalated": is_escalation,
                    "snapshot_path": snapshot_path,
                })

    # ------------------------------------------------------------------ #
    # VLM prompt construction
    # ------------------------------------------------------------------ #

    def _build_vlm_prompt(self, enabled: List[AlertConfig]) -> str:
        """
        Build a single multi-question JSON prompt for all enabled alerts.
        Batching all questions into one VLM call keeps inference costs low.
        """
        # Use json.dumps to safely serialise alert names/prompts (avoids
        # f-string injection if names contain quotes or special characters)
        questions = {a.name: a.prompt for a in enabled}
        example_output = {
            a.name: {"answer": "YES or NO", "reason": "brief explanation"}
            for a in enabled
        }

        return (
            "Analyse the provided image and answer ALL of the following questions.\n\n"
            f"QUESTIONS:\n{json.dumps(questions, indent=2)}\n\n"
            "RULES:\n"
            "1. For each question answer with EXACTLY \"YES\" or \"NO\" (uppercase).\n"
            "2. Provide a concise reason for each answer.\n"
            "3. Include EVERY question key in your response — do not omit any.\n\n"
            "OUTPUT (strict JSON, no markdown):\n"
            f"{json.dumps(example_output, indent=2)}"
        )

    # ------------------------------------------------------------------ #
    # VLM response parsing
    # ------------------------------------------------------------------ #

    def _parse_vlm_response(
        self, response: str, enabled: List[AlertConfig]
    ) -> Optional[dict]:
        """
        Clean, extract, and Pydantic-validate the VLM JSON response.
        Returns None if parsing fails entirely.
        """
        try:
            clean = response.replace("```json", "").replace("```", "").strip()
            start = clean.find("{")
            end = clean.rfind("}")
            if start == -1 or end == -1:
                logger.error(f"No JSON object found in VLM response: {response[:200]}")
                return None

            data = json.loads(clean[start:end + 1])

            validated: dict = {}
            for alert_cfg in enabled:
                raw = data.get(alert_cfg.name)
                if raw is None:
                    logger.warning(f"VLM omitted answer for alert '{alert_cfg.name}'")
                    validated[alert_cfg.name] = {"answer": "NO", "reason": "No response from VLM"}
                    continue
                try:
                    result = AgentResult(**raw)
                    validated[alert_cfg.name] = result.model_dump()
                except ValidationError as exc:
                    logger.warning(f"Validation failed for '{alert_cfg.name}': {exc}")
                    validated[alert_cfg.name] = {"answer": "NO", "reason": "Validation error"}

            return validated

        except json.JSONDecodeError as exc:
            logger.error(f"JSON decode error: {exc} | response={response[:300]}")
            return None
        except Exception as exc:
            logger.error(f"Unexpected parse error: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # Config persistence
    # ------------------------------------------------------------------ #

    def _load_alerts_config(self) -> List[AlertConfig]:
        """Load alert configurations from JSON; return defaults on failure."""
        path = self._agents_config_file
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    raw = json.load(fh)
                configs = []
                for entry in raw:
                    try:
                        configs.append(AlertConfig(**entry))
                    except ValidationError as exc:
                        logger.warning(f"Skipping invalid alert config entry: {exc}")
                if configs:
                    logger.info(f"Loaded {len(configs)} alert(s) from {path}")
                    return configs
            except Exception as exc:
                logger.error(f"Failed to load alert config: {exc}")

        logger.info("Using default alert configurations")
        return [
            AlertConfig(
                name="Fire Detection",
                prompt="Is there visible fire or smoke in the image?",
                enabled=True,
                severity=AlertSeverity.CRITICAL,
                cooldown_seconds=60,
                tools=["log_alert", "capture_snapshot", "send_email"],
            ),
            AlertConfig(
                name="Person Detection",
                prompt="Is there a person present in the frame?",
                enabled=True,
                severity=AlertSeverity.MEDIUM,
                cooldown_seconds=30,
                tools=["log_alert"],
            ),
        ]

    def _load_streams_config(self):
        path = self._streams_config_file
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    streams = json.load(fh)
                for s in streams:
                    self.add_stream(s["id"], s["url"], save=False)
                logger.info(f"Loaded {len(streams)} stream(s) from {path}")
            except Exception as exc:
                logger.error(f"Failed to load stream config: {exc}")

    def _save_streams_config(self):
        try:
            _RESOURCES.mkdir(parents=True, exist_ok=True)
            data = [{"id": sid, "url": m.rtsp_url} for sid, m in self.streams.items()]
            with open(self._streams_config_file, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.error(f"Failed to save stream config: {exc}")
