"""Deterministic stand-in for BedrockModel, for offline tests only (MODEL_PROVIDER=stub).

It speaks the same streaming event protocol as BedrockModel, so the real
Strands event loop, tool policy hooks, MCP tool calls and A2A server are all
exercised end to end. It is never used when MODEL_PROVIDER=bedrock.

Behaviour (keyed off the latest user text):
  * "ORD-1234 ..."                -> calls get_order_status, then summarises
  * "refund <amount> ... ORD-1234"-> calls calculate_refund
  * "policy|return|shipping|warranty" -> calls search_policy_kb
  * prompt-attack phrases         -> stopReason=guardrail_intervened (simulates
                                     Bedrock Guardrails blocking the input)
  * anything else                 -> plain text reply
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from strands.models.model import Model

ORDER_RE = re.compile(r"ORD-\d{4,8}", re.IGNORECASE)
AMOUNT_RE = re.compile(r"refund\D{0,20}(\d+(?:\.\d+)?)", re.IGNORECASE)
ATTACK_RE = re.compile(r"ignore (all|previous) instructions|system prompt|jailbreak", re.IGNORECASE)
BLOCKED_MESSAGE = "Sorry, I can't help with that request."


def _text_events(text: str, stop_reason: str = "end_turn") -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop_reason}},
    ]


def _tool_events(name: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    tool_use_id = f"tooluse_{uuid.uuid4().hex[:12]}"
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        texts = [block["text"] for block in message.get("content", []) if "text" in block]
        if texts:
            return " ".join(texts)
    return ""


def _pending_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not messages or messages[-1].get("role") != "user":
        return None
    for block in messages[-1].get("content", []):
        if "toolResult" in block:
            return block["toolResult"]
    return None


def _tool_result_text(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content", []):
        if "text" in item:
            parts.append(item["text"])
        elif "json" in item:
            parts.append(json.dumps(item["json"]))
    return " ".join(parts)


class StubModel(Model):
    def __init__(self, **config: Any):
        self.config = {"model_id": "stub", **config}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs) -> AsyncGenerator:
        raise NotImplementedError("StubModel does not support structured output")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterable[dict]:
        for event in self._plan(messages, {spec["name"] for spec in (tool_specs or [])}):
            yield event

    def _plan(self, messages: list[dict[str, Any]], tools: set[str]) -> list[dict[str, Any]]:
        result = _pending_tool_result(messages)
        if result is not None:
            body = _tool_result_text(result)
            if result.get("status") == "error":
                return _text_events(f"I could not complete that action: {body}")
            return _text_events(f"Here is what I found: {body}")

        text = _last_user_text(messages)
        if ATTACK_RE.search(text):
            return _text_events(BLOCKED_MESSAGE, stop_reason="guardrail_intervened")

        order = ORDER_RE.search(text)
        amount = AMOUNT_RE.search(text)
        if order and amount and "calculate_refund" in tools:
            return _tool_events("calculate_refund", {"order_id": order.group(0).upper(), "amount": float(amount.group(1))})
        if order and "get_order_status" in tools:
            return _tool_events("get_order_status", {"order_id": order.group(0).upper()})
        topic = re.search(r"return|shipping|warranty", text, re.IGNORECASE)
        if topic and "search_policy_kb" in tools:
            return _tool_events("search_policy_kb", {"topic": topic.group(0).lower()})
        return _text_events("Hello! I can check orders, calculate refunds and answer policy questions.")
