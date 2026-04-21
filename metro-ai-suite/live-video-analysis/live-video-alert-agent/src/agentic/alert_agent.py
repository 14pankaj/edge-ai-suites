# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.config import settings
from src.schemas.monitor import AlertConfig
from src.agentic.mcp_client import get_mcp_server_status, get_tool_defaults

logger = logging.getLogger(__name__)


_CONTEXT_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

_TOOLS_CONFIG_FILE = Path("resources/tools.json")
_TOOL_TIMEOUT = 10.0  # Per-tool execution timeout in seconds


def _load_tools_config() -> Tuple[Dict[str, Callable], List[dict]]:
    """
    Load tool registry from external JSON config file.
    
    Returns (tool_map, tool_schemas) where:
      - tool_map: {name: async_callable}
      - tool_schemas: OpenAI-compatible function schemas for LLM dispatch
    """
    tool_map: Dict[str, Callable] = {}
    tool_schemas: List[dict] = []

    if not _TOOLS_CONFIG_FILE.exists():
        logger.warning(f"Tools config not found: {_TOOLS_CONFIG_FILE} — using defaults")
        return {}, []

    try:
        with open(_TOOLS_CONFIG_FILE) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.error(f"Failed to load tools.json: {exc} — using defaults")
        return {}, []

    for tool in config:
        name = tool.get("name")
        if not name:
            continue

        # Check if tool is enabled
        if not tool.get("enabled", True):
            logger.info(f"Tool '{name}' is disabled in config — skipping")
            continue

        # Check required environment variables
        requires_env = tool.get("requires_env", [])
        missing = [e for e in requires_env if not os.getenv(e)]
        if missing:
            logger.debug(f"Tool '{name}' missing env vars {missing} — will skip at runtime")
            # Still register it; tool itself handles missing config gracefully

        # Dynamic import
        try:
            module_path = tool.get("module")
            func_name = tool.get("function")
            if not module_path or not func_name:
                logger.warning(f"Tool '{name}' missing module/function — skipping")
                continue

            module = importlib.import_module(module_path)
            fn = getattr(module, func_name)
            tool_map[name] = fn

            # Build OpenAI-compatible schema
            tool_schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", f"Execute {name} action"),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
            logger.debug(f"Loaded tool: {name} from {module_path}.{func_name}")

        except (ImportError, AttributeError) as exc:
            logger.error(f"Failed to load tool '{name}': {exc}")
            continue

    logger.info(f"Loaded {len(tool_map)} tools from {_TOOLS_CONFIG_FILE}")
    return tool_map, tool_schemas


_TOOL_MAP, _TOOL_SCHEMAS = _load_tools_config()

# MCP tools are merged in at runtime after MCP initialization
_MCP_TOOL_MAP: Dict[str, Callable] = {}
_MCP_TOOL_SCHEMAS: List[dict] = []
_MCP_TOOL_LOCK = threading.Lock()


def register_mcp_tools(tool_map: Dict[str, Callable], tool_schemas: List[dict]):
    """
    Register MCP tools with the alert agent.
    
    Called by main.py after MCP initialization.
    MCP tools are kept separate and merged during dispatch.
    """
    global _MCP_TOOL_MAP, _MCP_TOOL_SCHEMAS
    with _MCP_TOOL_LOCK:
        _MCP_TOOL_MAP = tool_map
        _MCP_TOOL_SCHEMAS = tool_schemas
    logger.info(f"Registered {len(tool_map)} MCP tools with AlertActionAgent")


def clear_mcp_tools():
    """Clear registered MCP tools (called during MCP shutdown/reload)."""
    global _MCP_TOOL_MAP, _MCP_TOOL_SCHEMAS
    with _MCP_TOOL_LOCK:
        _MCP_TOOL_MAP = {}
        _MCP_TOOL_SCHEMAS = []


def get_all_tools() -> Tuple[Dict[str, Callable], List[dict]]:
    """
    Get combined tool map and schemas (built-in + MCP).
    
    Returns (tool_map, tool_schemas) for all available tools.
    """
    with _MCP_TOOL_LOCK:
        combined_map = {**_TOOL_MAP, **_MCP_TOOL_MAP}
        combined_schemas = _TOOL_SCHEMAS + _MCP_TOOL_SCHEMAS
    return combined_map, combined_schemas


def reload_tools() -> int:
    """
    Reload built-in tools from tools.json at runtime.
    
    Returns the number of built-in tools loaded.
    Note: MCP tools are reloaded separately via /mcp/reload endpoint.
    """
    global _TOOL_MAP, _TOOL_SCHEMAS
    _TOOL_MAP, _TOOL_SCHEMAS = _load_tools_config()
    return len(_TOOL_MAP)


def get_available_tools() -> List[dict]:
    """
    Return list of all available tools with their metadata.
    
    Includes both built-in tools and MCP tools.
    Useful for API introspection (GET /tools endpoint).
    """
    result = []
    
    # Built-in tools
    for schema in _TOOL_SCHEMAS:
        func = schema.get("function", {})
        result.append({
            "name": func.get("name"),
            "description": func.get("description"),
            "enabled": func.get("name") in _TOOL_MAP,
            "source": "builtin",
        })
    
    # MCP tools
    for schema in _MCP_TOOL_SCHEMAS:
        func = schema.get("function", {})
        result.append({
            "name": func.get("name"),
            "description": func.get("description"),
            "enabled": func.get("name") in _MCP_TOOL_MAP,
            "source": "mcp",
        })
    
    return result


class AlertActionAgent:
    """
    Dispatches actions when an alert fires.

    When USE_ADK=true, uses the Google ADK framework backed by the local OVMS
    endpoint (LOCAL_LLM_URL).  When false, falls back to rule-based dispatch.
    """

    def __init__(self, use_adk: Optional[bool] = None):
        self._use_adk = use_adk if use_adk is not None else settings.USE_ADK
        self._adk_runner = None
        self._session_service = None
        self._known_sessions: set = set()

        if self._use_adk:
            self._init_adk()
        else:
            logger.info("AlertActionAgent initialised in rule-based mode")

    def _init_adk(self, preserve_sessions: bool = False):
        """Initialise the Google ADK runner backed by local OVMS via LiteLLM.

        When preserve_sessions=True the existing session service is reused so
        that in-flight sessions survive a tool-list refresh.
        """
        try:
            from google.adk.agents import LlmAgent
            from google.adk.models.lite_llm import LiteLlm
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.tools import FunctionTool

            if not settings.LOCAL_LLM_URL:
                logger.warning(
                    "USE_ADK=true but LOCAL_LLM_URL is not set — "
                    "falling back to rule-based mode"
                )
                self._use_adk = False
                return

            # LiteLlm proxies to the local OVMS OpenAI-compatible endpoint.
            os.environ.setdefault("LITELLM_PROXY_API_KEY", "local")
            os.environ.setdefault("LITELLM_PROXY_API_BASE", settings.LOCAL_LLM_URL)
            LiteLlm.use_litellm_proxy = True
            adk_model = LiteLlm(model=f"litellm_proxy/{settings.LOCAL_LLM_MODEL}")
            logger.info(
                f"ADK using local OVMS (url={settings.LOCAL_LLM_URL} "
                f"model={settings.LOCAL_LLM_MODEL})"
            )

            all_tools, all_schemas = get_all_tools()

            builtin_tools_desc = []
            mcp_tools_desc = []
            for schema in all_schemas:
                func = schema.get("function", {})
                name = func.get("name", "")
                desc = func.get("description", "")
                params = func.get("parameters", {}).get("properties", {})
                param_str = ", ".join(params.keys()) if params else "none"
                entry = f"  - {name}: {desc} (params: {param_str})"
                if name.startswith("mcp_"):
                    mcp_tools_desc.append(entry)
                else:
                    builtin_tools_desc.append(entry)

            builtin_section = "BUILT-IN TOOLS:\n" + "\n".join(builtin_tools_desc) if builtin_tools_desc else ""

            mcp_section = ""
            if mcp_tools_desc:
                # Gather connected MCP server info
                try:
                    servers = get_mcp_server_status()
                    server_lines = "\n".join(
                        f"  - {s['name']} ({s['transport']}:{s.get('url', 'stdio')}) connected={s['connected']}"
                        for s in servers
                    )
                except Exception:
                    server_lines = "  (server info unavailable)"

                mcp_section = (
                    f"\n\nMCP SERVERS (external integrations):\n{server_lines}\n\n"
                    "MCP TOOLS (provided by the above servers):\n"
                    + "\n".join(mcp_tools_desc)
                    + "\n\n"
                    "MCP TOOL GUIDELINES:\n"
                    "- MCP tool names are prefixed with 'mcp_<server>_<tool>'\n"
                    "- Use MCP tools when the alert context benefits from external data "
                    "(e.g. querying metrics, checking system health)\n"
                    "- If a configured_tools list includes an MCP tool, invoke it as you would any other tool\n"
                )

            instruction = (
                "You are a security alert action agent for a live video surveillance system.\n\n"
                "TASK: When you receive an alert detection, analyze the severity, context, "
                "and available tools, then invoke the most appropriate combination of tools "
                "to handle the alert effectively.\n\n"
                f"{builtin_section}{mcp_section}\n\n"
                "RULES:\n"
                "1. ALWAYS invoke log_alert for every alert.\n"
                "2. Use ALL available tools that are relevant to the alert context — "
                "you are not limited to the configured_tools list.\n"
                "3. Select tools based on severity and context:\n"
                "   - CRITICAL: log_alert + capture_snapshot + send_email (minimum); "
                "consider trigger_webhook and publish_mqtt as well\n"
                "   - HIGH: log_alert + capture_snapshot; add send_email if escalated; "
                "consider MCP tools for additional context\n"
                "   - MEDIUM: log_alert + any tools that add value (e.g. webhook, MCP queries)\n"
                "   - LOW: log_alert; optionally add other tools if useful\n"
                "4. If escalated=true: invoke trigger_webhook and publish_mqtt in addition.\n"
                "5. Use MCP tools proactively when they can enrich the alert response "
                "(e.g. query system metrics, check infrastructure health, gather correlated data).\n"
                "6. Pass relevant alert context (stream_id, alert_name, severity, reason) "
                "as arguments to MCP tools.\n\n"
                "After invoking tools, return a one-line summary of actions taken."
            )

            # Create FunctionTools for all available tools (built-in + MCP)
            adk_tools = [FunctionTool(fn) for fn in all_tools.values()]

            agent = LlmAgent(
                name="alert_action_agent",
                model=adk_model,
                description="Processes video alert detections and dispatches actions",
                instruction=instruction,
                tools=adk_tools,
            )

            # Reuse existing session service when refreshing tools so that
            # in-flight alert sessions are not silently dropped.
            if preserve_sessions and self._session_service is not None:
                session_service = self._session_service
            else:
                session_service = InMemorySessionService()
                self._known_sessions.clear()

            self._session_service = session_service
            self._adk_runner = Runner(
                agent=agent,
                app_name="live-video-alert-agent",
                session_service=session_service,
            )
            logger.info(f"AlertActionAgent initialised with ADK (model=local:{settings.LOCAL_LLM_MODEL})")

        except ImportError as exc:
            logger.warning(
                f"google-adk not installed or import failed ({exc}) — "
                "falling back to rule-based mode"
            )
            self._use_adk = False
        except Exception as exc:
            logger.error(f"ADK init error: {exc} — falling back to rule-based mode")
            self._use_adk = False

    def reinit_adk(self):
        """Re-initialise the ADK runner with the current tool set.

        Call after MCP tools are registered so the agent instruction and
        FunctionTool list include all available tools.  The existing session
        service is preserved so in-flight sessions are not disrupted.
        """
        if not self._use_adk:
            return
        logger.info("Re-initialising ADK agent with updated tool set ...")
        self._init_adk(preserve_sessions=True)

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
            logger.info(
                f"[DISPATCH] mode=adk-local stream={stream_id} alert={alert_cfg.name} "
                f"severity={alert_cfg.severity.value} escalated={escalated}"
            )
            return await self._dispatch_adk(
                stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )
        else:
            logger.info(
                f"[DISPATCH] mode=rule_based stream={stream_id} alert={alert_cfg.name} "
                f"severity={alert_cfg.severity.value} escalated={escalated}"
            )
            return await self._dispatch_rule_based(
                stream_id, alert_cfg, answer, reason,
                consecutive_count, escalated, snapshot_path,
            )

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

        common_ctx = {
            "stream_id": stream_id,
            "alert_name": alert_cfg.name,
            "severity": alert_cfg.severity.value,
            "answer": answer,
            "reason": reason,
            "consecutive_count": consecutive_count,
            "escalated": escalated,
            "snapshot_path": snapshot_path,
        }

        # Get combined tool map (built-in + MCP)
        all_tools, _ = get_all_tools()

        # --- Prepare all tool calls (synchronous kwarg building) ---
        prepared: List[Tuple[str, Callable, dict]] = []
        for tool_name in names:
            fn = all_tools.get(tool_name)
            if fn is None:
                logger.warning(f"Unknown tool '{tool_name}' — skipped")
                continue
            try:
                if tool_name.startswith("mcp_"):
                    configured_args = alert_cfg.tool_arguments.get(tool_name, {})
                    if configured_args:
                        kwargs = _render_tool_arguments(configured_args, common_ctx)
                    else:
                        kwargs = _render_tool_arguments(
                            get_tool_defaults(tool_name), common_ctx,
                        )
                else:
                    kwargs = _build_tool_kwargs(
                        tool_name, common_ctx, consecutive_count, escalated, snapshot_path,
                    )
                    override_args = _render_tool_arguments(
                        alert_cfg.tool_arguments.get(tool_name, {}),
                        common_ctx,
                    )
                    if override_args:
                        kwargs.update(override_args)
                prepared.append((tool_name, fn, kwargs))
            except Exception as exc:
                logger.error(f"Failed to prepare tool '{tool_name}': {exc}")

        # --- Execute all tools in parallel with per-tool timeout ---
        async def _run_one(name: str, fn: Callable, kwargs: dict) -> Tuple[str, bool]:
            try:
                result = await asyncio.wait_for(fn(**kwargs), timeout=_TOOL_TIMEOUT)
                logger.debug(f"Tool '{name}' result: {result}")
                return name, result.get("status") != "error"
            except asyncio.TimeoutError:
                logger.error(f"Tool '{name}' timed out after {_TOOL_TIMEOUT}s")
                return name, False
            except Exception as exc:
                logger.error(f"Tool '{name}' raised: {exc}")
                return name, False

        results = await asyncio.gather(
            *[_run_one(n, f, k) for n, f, k in prepared]
        )
        return [name for name, ok in results if ok]

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
            session_id = f"{stream_id}_{alert_cfg.name}".replace(" ", "_")

            # Create the session if it doesn't exist yet
            if session_id not in self._known_sessions:
                await self._session_service.create_session(
                    app_name="live-video-alert-agent",
                    user_id="system",
                    session_id=session_id,
                )
                self._known_sessions.add(session_id)

            logger.info(
                f"[ADK] Sending to ADK agent — stream={stream_id} "
                f"alert={alert_cfg.name} model={settings.LOCAL_LLM_MODEL} "
                f"session={session_id}"
            )

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
                from google.genai import types
                events = self._adk_runner.run(
                    user_id="system",
                    session_id=session_id,
                    new_message=types.Content(parts=[types.Part(text=prompt)]),
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


def _render_tool_arguments(value: Any, ctx: Dict[str, Any]) -> Any:
    """Render template placeholders in tool arguments from the alert context."""
    if isinstance(value, str):
        return _CONTEXT_TEMPLATE_PATTERN.sub(
            lambda m: "" if ctx.get(m.group(1)) is None else str(ctx.get(m.group(1))),
            value,
        )
    if isinstance(value, list):
        return [_render_tool_arguments(item, ctx) for item in value]
    if isinstance(value, dict):
        return {k: _render_tool_arguments(v, ctx) for k, v in value.items()}
    return value
