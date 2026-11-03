"""Application-level guardrails implemented as Strands hooks.

Three layers of guardrails exist in this PoC:

1. Amazon Bedrock Guardrails  -> model input/output (content filters, prompt
   attacks, denied topics, PII). Configured on BedrockModel (agent_factory.py).
2. ToolPolicyHook (this file) -> what the agent may do with tools: allowlist,
   per-turn call budget, business rules (refunds above a limit need a human).
   The managed equivalent at scale is AgentCore Policy (Cedar) on AgentCore Gateway.
3. Tool-side validation        -> the MCP server validates its own inputs.

GuardrailMetricsHook publishes custom CloudWatch metrics in Embedded Metric
Format (EMF) so interventions and policy denials show up on the dashboard and
can be alarmed on.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from .config import Settings

logger = logging.getLogger("agent.policy")


def emit_metric(settings: Settings, name: str, value: float = 1, **dims: str) -> None:
    """Write one EMF record to stdout. CloudWatch Logs extracts it as a metric."""
    dimensions = {"Service": settings.agent_name, **dims}
    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": settings.metrics_namespace,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": name, "Unit": "Count"}],
                }
            ],
        },
        **dimensions,
        name: value,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


class ToolPolicyHook(HookProvider):
    def __init__(self, settings: Settings):
        self.settings = settings
        self._calls = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._reset)
        registry.add_callback(BeforeToolCallEvent, self._check)

    def _reset(self, _event: BeforeInvocationEvent) -> None:
        self._calls = 0

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Return a denial reason, or None to allow. Pure function for testing."""
        if tool_name not in self.settings.allowed_tools:
            return f"Tool '{tool_name}' is not on the approved tool list."
        if self._calls >= self.settings.max_tool_calls:
            return f"Tool call budget of {self.settings.max_tool_calls} per request exhausted."
        if tool_name == "calculate_refund":
            try:
                amount = float(tool_input.get("amount", 0))
            except (TypeError, ValueError):
                return "Refund amount must be a number."
            if amount > self.settings.refund_approval_limit:
                return (
                    f"Refunds above {self.settings.refund_approval_limit:.0f} require human approval. "
                    "Tell the user the request was escalated to a support agent."
                )
        return None

    def _check(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        reason = self.decide(name, event.tool_use.get("input") or {})
        self._calls += 1
        if reason:
            logger.warning("tool denied tool=%s reason=%s", name, reason)
            emit_metric(self.settings, "ToolCallsDenied", Tool=name)
            event.cancel_tool = reason
        else:
            emit_metric(self.settings, "ToolCallsAllowed", Tool=name)


class GuardrailMetricsHook(HookProvider):
    def __init__(self, settings: Settings):
        self.settings = settings

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self._after_model)

    def _after_model(self, event: AfterModelCallEvent) -> None:
        if event.exception is not None:
            emit_metric(self.settings, "ModelCallErrors")
            return
        if event.stop_response and event.stop_response.stop_reason == "guardrail_intervened":
            logger.warning("bedrock guardrail intervened")
            emit_metric(self.settings, "GuardrailInterventions")
