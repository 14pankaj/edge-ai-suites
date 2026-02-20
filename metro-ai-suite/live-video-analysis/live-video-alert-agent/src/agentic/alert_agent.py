"""
AlertActionAgent — Google ADK-backed agentic action dispatcher.

Architecture
------------
When an alert fires (YES answer from VLM), this agent decides *which* tools
to invoke and *how* to invoke them, using one of two execution modes:

ADK mode (USE_ADK=true + GEMINI_API_KEY set)
    A Google ADK LlmAgent receives structured alert context and uses an LLM
    (Gemini by default) to reason about the appropriate response, then
    invokes the registered tool functions.  This enables natural-language
    customisation of escalation logic without code changes.

Rule mode (default / fallback)
    The configured tool list from AlertConfig is executed directly in order.
    No external LLM is required — works fully offline / air-gapped.

In both modes the same five async tool functions are used:
    log_alert, send_email_alert, trigger_webhook, capture_snapshot, publish_mqtt

Usage (called from AgentManager)
-----------------------------------
    agent = AlertActionAgent()
    results = await agent.dispatch(
        stream_id="cam1",
        alert_cfg=<AlertConfig>,
        answer="YES",
        reason="Flames visible",
        consecutive_count=3,
        escalated=False,
        snapshot_path="/snapshots/cam1/Fire_2026.jpg",
    )
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.config import settings
from src.schemas.monitor import AlertConfig

from .tools.log_tool import log_alert
from .tools.email_tool import send_email_alert
from .tools.webhook_tool import trigger_webhook
from .tools.snapshot_tool import capture_snapshot
from .tools.mqtt_tool import publish_mqtt

logger = logging.getLogger(__name__)

# Map tool name strings (stored in AlertConfig.tools) → async callables
_TOOL_MAP = {
    "log_alert": log_alert,
    "send_email": send_email_alert,
    "trigger_webhook": trigger_webhook,
    "capture_snapshot": capture_snapshot,
    "publish_mqtt": publish_mqtt,
}


class AlertActionAgent:
    """
    Dispatches actions when an alert fires.

    Parameters
    ----------
    use_adk:
        Override the USE_ADK setting.  If None, uses settings.USE_ADK.
    """

    def __init__(self, use_adk: Optional[bool] = None):
        self._use_adk = use_adk if use_adk is not None else settings.USE_ADK
        self._adk_runner = None

        if self._use_adk:
            self._init_adk()
        else:
            logger.info("AlertActionAgent initialised in rule-based mode")

    # ------------------------------------------------------------------ #
    # ADK initialisation
    # ------------------------------------------------------------------ #

    def _init_adk(self):
        """Initialise the Google ADK runner lazily."""
        try:
            import google.generativeai as genai
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.tools import FunctionTool

            if not settings.GEMINI_API_KEY:
                logger.warning(
                    "USE_ADK=true but GEMINI_API_KEY is not set — "
                    "falling back to rule-based mode"
                )
                self._use_adk = False
                return

            genai.configure(api_key=settings.GEMINI_API_KEY)

            instruction = (
                "You are an intelligent security alert action agent embedded in a "
                "video surveillance system. "
                "When you receive an alert detection result you must decide which "
                "tools to call to handle the situation appropriately. "
                "Follow these guidelines:\n"
                "- CRITICAL severity: always call capture_snapshot, log_alert, "
                "  and send_email at minimum.\n"
                "- HIGH severity: call capture_snapshot and log_alert. "
                "  Call send_email if the alert is new or escalating.\n"
                "- MEDIUM severity: call log_alert. Optionally call trigger_webhook.\n"
                "- LOW severity: call log_alert only.\n"
                "- If escalated=true, include trigger_webhook and publish_mqtt "
                "  regardless of severity.\n"
                "- Only call tools listed in configured_tools.\n"
                "- Always call log_alert.\n"
                "- Return a brief summary of the actions you took."
            )

            adk_tools = [FunctionTool(fn) for fn in _TOOL_MAP.values()]

            agent = LlmAgent(
                name="alert_action_agent",
                model=settings.ADK_MODEL,
                description="Processes video alert detections and dispatches actions",
                instruction=instruction,
                tools=adk_tools,
            )

            session_service = InMemorySessionService()
            self._adk_runner = Runner(
                agent=agent,
                app_name="live-video-alert",
                session_service=session_service,
            )
            logger.info(
                f"AlertActionAgent initialised with ADK (model={settings.ADK_MODEL})"
            )

        except ImportError as exc:
            logger.warning(
                f"google-adk not installed or import failed ({exc}) — "
                "falling back to rule-based mode"
            )
            self._use_adk = False
        except Exception as exc:
            logger.error(f"ADK init error: {exc} — falling back to rule-based mode")
            self._use_adk = False

    # ------------------------------------------------------------------ #
    # Public dispatch
    # ------------------------------------------------------------------ #

    async def dispatch(
        self,
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,
        reason: str,
        consecutive_count: int = 1,
        escalated: bool = False,
        snapshot_path: Optional[str] = None,
    ) -> List[str]:
        """
        Execute actions for a triggered alert.

        Returns a list of tool names that were successfully invoked.
        """
        if answer != "YES":
            return []

        if self._use_adk and self._adk_runner:
            return await self._dispatch_adk(
                stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )
        else:
            return await self._dispatch_rule_based(
                stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )

    # ------------------------------------------------------------------ #
    # Rule-based executor
    # ------------------------------------------------------------------ #

    async def _dispatch_rule_based(
        self,
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,
        reason: str,
        consecutive_count: int,
        escalated: bool,
        snapshot_path: Optional[str],
    ) -> List[str]:
        """
        Directly invoke the tools listed in alert_cfg.tools (and escalation
        tools if applicable) without an LLM reasoning step.
        """
        tool_names: List[str] = list(alert_cfg.tools)

        # Always ensure log_alert is present
        if "log_alert" not in tool_names:
            tool_names.insert(0, "log_alert")

        # Add escalation tools if escalated
        if escalated and alert_cfg.escalation:
            for t in alert_cfg.escalation.additional_tools:
                if t not in tool_names:
                    tool_names.append(t)

        invoked: List[str] = []
        common_ctx = {
            "stream_id": stream_id,
            "alert_name": alert_cfg.name,
            "severity": alert_cfg.severity.value,
            "answer": answer,
            "reason": reason,
        }

        for tool_name in tool_names:
            fn = _TOOL_MAP.get(tool_name)
            if fn is None:
                logger.warning(f"Unknown tool '{tool_name}' in alert '{alert_cfg.name}'")
                continue
            try:
                kwargs = _build_tool_kwargs(tool_name, common_ctx, consecutive_count, escalated, snapshot_path)
                result = await fn(**kwargs)
                if result.get("status") not in ("error",):
                    invoked.append(tool_name)
                logger.debug(f"Tool '{tool_name}' result: {result}")
            except Exception as exc:
                logger.error(f"Tool '{tool_name}' raised exception: {exc}")

        return invoked

    # ------------------------------------------------------------------ #
    # ADK executor
    # ------------------------------------------------------------------ #

    async def _dispatch_adk(
        self,
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,
        reason: str,
        consecutive_count: int,
        escalated: bool,
        snapshot_path: Optional[str],
    ) -> List[str]:
        """
        Feed alert context into the ADK agent and let it decide tool calls.
        """
        try:
            from google.adk.sessions import InMemorySessionService
            import google.generativeai.types as genai_types

            session_id = f"{stream_id}_{alert_cfg.name}".replace(" ", "_")

            prompt = (
                f"Alert detection result:\n"
                f"  stream_id: {stream_id}\n"
                f"  alert_name: {alert_cfg.name}\n"
                f"  severity: {alert_cfg.severity.value}\n"
                f"  answer: {answer}\n"
                f"  reason: {reason}\n"
                f"  consecutive_count: {consecutive_count}\n"
                f"  escalated: {escalated}\n"
                f"  snapshot_path: {snapshot_path or 'none'}\n"
                f"  configured_tools: {alert_cfg.tools}\n"
                f"\nPlease handle this alert appropriately."
            )

            # ADK runner.run() is synchronous — run in thread pool
            invoked_tools: List[str] = []

            def _run_adk():
                from google.adk.types import Content, Part
                events = self._adk_runner.run(
                    user_id="system",
                    session_id=session_id,
                    new_message=Content(parts=[Part(text=prompt)]),
                )
                called = []
                for event in events:
                    if hasattr(event, "tool_call") and event.tool_call:
                        called.append(event.tool_call.name)
                return called

            invoked_tools = await asyncio.to_thread(_run_adk)
            logger.info(
                f"ADK agent invoked tools for [{stream_id}][{alert_cfg.name}]: "
                f"{invoked_tools}"
            )
            return invoked_tools

        except Exception as exc:
            logger.error(f"ADK dispatch failed: {exc} — falling back to rule-based")
            return await self._dispatch_rule_based(
                stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )


# ------------------------------------------------------------------ #
# Helper
# ------------------------------------------------------------------ #

def _build_tool_kwargs(
    tool_name: str,
    ctx: Dict[str, Any],
    consecutive_count: int,
    escalated: bool,
    snapshot_path: Optional[str],
) -> Dict[str, Any]:
    """Map common alert context fields to per-tool keyword arguments."""
    base = {
        "stream_id": ctx["stream_id"],
        "alert_name": ctx["alert_name"],
        "severity": ctx["severity"],
    }
    if tool_name == "log_alert":
        return {
            **base,
            "answer": ctx["answer"],
            "reason": ctx["reason"],
            "consecutive_count": consecutive_count,
            "escalated": escalated,
            "snapshot_path": snapshot_path,
        }
    if tool_name == "send_email":
        severity = ctx["severity"].upper()
        return {
            "subject": f"[{severity}] {ctx['alert_name']} — {ctx['stream_id']}",
            "body": (
                f"Alert: {ctx['alert_name']}\n"
                f"Stream: {ctx['stream_id']}\n"
                f"Severity: {ctx['severity']}\n"
                f"Answer: {ctx['answer']}\n"
                f"Reason: {ctx['reason']}\n"
                f"Consecutive detections: {consecutive_count}\n"
                f"Escalated: {escalated}\n"
                + (f"Snapshot: {snapshot_path}\n" if snapshot_path else "")
            ),
            **{k: base[k] for k in ("stream_id", "alert_name", "severity")},
        }
    if tool_name == "trigger_webhook":
        return {
            "payload": {
                **ctx,
                "consecutive_count": consecutive_count,
                "escalated": escalated,
                "snapshot_path": snapshot_path,
            }
        }
    if tool_name == "capture_snapshot":
        return base
    if tool_name == "publish_mqtt":
        return {
            **base,
            "answer": ctx["answer"],
            "reason": ctx["reason"],
        }
    return {}
