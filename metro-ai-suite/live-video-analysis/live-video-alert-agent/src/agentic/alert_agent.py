# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

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
import json
import logging
import re
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

# OpenAI-compatible tool schemas for the local-LLM dispatch mode.
# Parameters are intentionally empty: the LLM selects *which* tools to invoke;
# kwargs are populated automatically by _build_tool_kwargs().
_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "log_alert",
            "description": "Log the alert event to application logs and history. Always invoke.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email notification about the alert to configured recipients.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_webhook",
            "description": "Send an HTTP POST webhook notification about the alert.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_snapshot",
            "description": "Save a snapshot image of the current camera frame to disk.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_mqtt",
            "description": "Publish the alert event to an MQTT broker.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _parse_tool_names_from_text(text: str) -> List[str]:
    """Extract valid tool names from free-form LLM text output."""
    valid = list(_TOOL_MAP.keys())
    valid_set = set(valid)
    # Try to find a JSON array anywhere in the response
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        try:
            candidates = json.loads(match.group())
            if isinstance(candidates, list):
                # Preserve model-returned order while deduplicating.
                found = []
                for t in candidates:
                    if isinstance(t, str) and t in valid_set and t not in found:
                        found.append(t)
                if found:
                    return found
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: look for any known tool name mentioned in the text
    lowered = text.lower()
    return [name for name in valid if name.lower() in lowered]


class AlertActionAgent:
    """
    Dispatches actions when an alert fires.

    Parameters
    ----------
    use_adk:
        Override the USE_ADK setting.  If None, uses settings.USE_ADK.
    """

    def __init__(
        self,
        use_adk: Optional[bool] = None,
        use_local_llm: Optional[bool] = None,
    ):
        self._use_adk = use_adk if use_adk is not None else settings.USE_ADK
        # USE_ADK takes precedence; local LLM only activates when ADK is off
        self._use_local_llm = (
            (use_local_llm if use_local_llm is not None else settings.USE_LOCAL_LLM)
            and not self._use_adk
        )
        self._adk_runner = None
        self._local_llm_client = None

        if self._use_adk:
            self._init_adk()
        elif self._use_local_llm:
            self._init_local_llm()
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

    def _init_local_llm(self):
        """
        Initialise an AsyncOpenAI client pointing at a locally hosted
        OpenAI-compatible endpoint (OVMS text model service).

        The ``openai`` package is already a project dependency (used by
        VlmClient), so no additional install is required.
        """
        try:
            from openai import AsyncOpenAI  # already in requirements.txt

            if not settings.LOCAL_LLM_URL:
                logger.warning(
                    "USE_LOCAL_LLM=true but LOCAL_LLM_URL is not set — "
                    "falling back to rule-based mode"
                )
                self._use_local_llm = False
                return

            self._local_llm_client = AsyncOpenAI(
                base_url=settings.LOCAL_LLM_URL,
                api_key=settings.LOCAL_LLM_API_KEY or "local",
            )
            logger.info(
                f"AlertActionAgent initialised with local LLM "
                f"(url={settings.LOCAL_LLM_URL} model={settings.LOCAL_LLM_MODEL})"
            )
        except Exception as exc:
            logger.error(
                f"Local LLM init error: {exc} — falling back to rule-based mode"
            )
            self._use_local_llm = False

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
        elif self._use_local_llm and self._local_llm_client:
            return await self._dispatch_local_llm(
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
        if "log_alert" not in tool_names:
            tool_names.insert(0, "log_alert")
        if escalated and alert_cfg.escalation:
            for t in alert_cfg.escalation.additional_tools:
                if t not in tool_names:
                    tool_names.append(t)
        return await self._execute_tool_list(
            tool_names, stream_id, alert_cfg, answer, reason,
            consecutive_count, escalated, snapshot_path,
        )

    async def _execute_tool_list(
        self,
        tool_names: List[str],
        stream_id: str,
        alert_cfg: AlertConfig,
        answer: str,
        reason: str,
        consecutive_count: int,
        escalated: bool,
        snapshot_path: Optional[str],
    ) -> List[str]:
        """
        Execute a specific list of tools, building all kwargs automatically.
        Shared by rule-based and local-LLM dispatch modes.
        """
        names = list(tool_names)
        if "log_alert" not in names:
            names.insert(0, "log_alert")

        invoked: List[str] = []
        common_ctx = {
            "stream_id": stream_id,
            "alert_name": alert_cfg.name,
            "severity": alert_cfg.severity.value,
            "answer": answer,
            "reason": reason,
        }
        for tool_name in names:
            fn = _TOOL_MAP.get(tool_name)
            if fn is None:
                logger.warning(f"Unknown tool '{tool_name}' — skipped")
                continue
            try:
                kwargs = _build_tool_kwargs(
                    tool_name, common_ctx, consecutive_count, escalated, snapshot_path,
                )
                result = await fn(**kwargs)
                if result.get("status") != "error":
                    invoked.append(tool_name)
                logger.debug(f"Tool '{tool_name}' result: {result}")
            except Exception as exc:
                logger.error(f"Tool '{tool_name}' raised: {exc}")
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
    # Local LLM executor
    # ------------------------------------------------------------------ #

    async def _dispatch_local_llm(
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
        Use a locally hosted OpenAI-compatible text LLM to decide which tools
        to invoke.

        Strategy
        --------
        1. **Tool-calling** — send the alert context with ``tools=`` populated
           from ``_TOOL_SCHEMAS``.  Models that support function-calling
              (for example Phi-4, Phi-3, Mistral served by OVMS) will return
           ``tool_calls`` directly.
        2. **JSON fallback** — if tool-calling is unsupported or returns no
           calls, re-send the prompt asking for a plain JSON array of tool
           names.  ``_parse_tool_names_from_text()`` handles imperfect output.
        3. **Rule-based fallback** — used when the LLM returns nothing useful
           or any exception is raised.
        """
        try:
            # Build the list of tools the LLM may select from
            available: List[str] = list(alert_cfg.tools)
            if "log_alert" not in available:
                available.insert(0, "log_alert")
            if escalated and alert_cfg.escalation:
                for t in alert_cfg.escalation.additional_tools:
                    if t not in available:
                        available.append(t)

            system_msg = (
                "You are a security alert action agent in a video surveillance system.\n"
                "Decide which tools to invoke based on the alert details provided.\n"
                "Guidelines:\n"
                "- CRITICAL severity: invoke capture_snapshot, log_alert, send_email.\n"
                "- HIGH severity: invoke capture_snapshot and log_alert; "
                "  send_email if new or escalating.\n"
                "- MEDIUM severity: invoke log_alert; optionally trigger_webhook.\n"
                "- LOW severity: invoke log_alert only.\n"
                "- If escalated=true: also invoke trigger_webhook and publish_mqtt.\n"
                "- Select ONLY tools listed in configured_tools.\n"
                "- ALWAYS include log_alert.\n"
            )
            user_content = (
                f"stream_id: {stream_id}\n"
                f"alert_name: {alert_cfg.name}\n"
                f"severity: {alert_cfg.severity.value}\n"
                f"answer: {answer}\n"
                f"reason: {reason}\n"
                f"consecutive_count: {consecutive_count}\n"
                f"escalated: {escalated}\n"
                f"snapshot_path: {snapshot_path or 'none'}\n"
                f"configured_tools: {available}\n"
            )
            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_content},
            ]
            # Only expose schemas for tools the alert is configured to use
            filtered_schemas = [
                s for s in _TOOL_SCHEMAS
                if s["function"]["name"] in available
            ]

            selected_tools: List[str] = []

            # --- Attempt 1: tool-calling (function-calling) API ---
            try:
                resp = await self._local_llm_client.chat.completions.create(
                    model=settings.LOCAL_LLM_MODEL,
                    messages=messages,
                    tools=filtered_schemas,
                    tool_choice="auto",
                    timeout=settings.LOCAL_LLM_TIMEOUT,
                )
                msg = resp.choices[0].message
                if msg.tool_calls:
                    selected_tools = [
                        tc.function.name for tc in msg.tool_calls
                        if tc.function.name in _TOOL_MAP
                    ]
                    logger.info(
                        f"[{stream_id}][{alert_cfg.name}] Local LLM "
                        f"tool-calls: {selected_tools}"
                    )
            except Exception as tc_exc:
                logger.debug(
                    f"Tool-calling API unavailable ({tc_exc}); "
                    "trying JSON text fallback"
                )

            # --- Attempt 2: JSON text fallback ---
            if not selected_tools:
                json_prompt = (
                    user_content
                    + f"\nReturn ONLY a JSON array of tool names to invoke "
                    f"from this list: {available}\n"
                    f'Example: ["log_alert", "send_email"]'
                )
                resp = await self._local_llm_client.chat.completions.create(
                    model=settings.LOCAL_LLM_MODEL,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": json_prompt},
                    ],
                    timeout=settings.LOCAL_LLM_TIMEOUT,
                )
                raw = (resp.choices[0].message.content or "").strip()
                selected_tools = _parse_tool_names_from_text(raw)
                logger.info(
                    f"[{stream_id}][{alert_cfg.name}] Local LLM JSON-parsed "
                    f"tools: {selected_tools} (raw: {raw[:120]})"
                )

            if not selected_tools:
                logger.warning(
                    f"Local LLM returned no valid tool names for "
                    f"[{stream_id}][{alert_cfg.name}] — falling back to rule-based"
                )
                return await self._dispatch_rule_based(
                    stream_id, alert_cfg, answer, reason,
                    consecutive_count, escalated, snapshot_path,
                )

            return await self._execute_tool_list(
                selected_tools, stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )

        except Exception as exc:
            logger.error(
                f"Local LLM dispatch failed for [{stream_id}][{alert_cfg.name}]: "
                f"{exc} — falling back to rule-based"
            )
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
