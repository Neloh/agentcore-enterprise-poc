"""Builds one Strands Agent per A2A context (conversation).

StrandsA2AExecutor calls make_agent(context_id) once per context, so each
conversation gets isolated state. The MCP client is shared across agents: the
MCP server is stateless, and opening one connection per conversation would add
latency to every first turn.
"""

from __future__ import annotations

import logging
import threading

from strands import Agent
from strands.models import BedrockModel
from strands.models.model import Model
from strands.tools.mcp import MCPClient

from .config import Settings
from .policy import GuardrailMetricsHook, ToolPolicyHook
from .sigv4_auth import SigV4HttpxAuth
from .stub_model import StubModel

logger = logging.getLogger("agent.factory")

SYSTEM_PROMPT = """You are the customer-support agent for an online retailer.
Use the provided tools to look up orders, calculate refunds and quote policy.
Never invent order data. If a tool call is denied by policy, explain the denial
to the user in plain language. Keep answers short and factual."""


def build_model(settings: Settings) -> Model:
    if settings.model_provider == "stub":
        return StubModel()
    kwargs = {}
    if settings.guardrails_enabled:
        kwargs.update(
            guardrail_id=settings.guardrail_id,
            guardrail_version=settings.guardrail_version,
            guardrail_trace="enabled",
            # Replace blocked model output instead of returning it to the caller.
            guardrail_redact_output=True,
            guardrail_redact_output_message="Sorry, I can't help with that request.",
        )
    else:
        logger.warning("Bedrock Guardrails NOT configured (GUARDRAIL_ID/GUARDRAIL_VERSION unset)")
    return BedrockModel(model_id=settings.model_id, region_name=settings.aws_region, **kwargs)


class ToolRegistry:
    """Lazily starts the shared MCP client; retries on the next request if MCP is down."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._client: MCPClient | None = None
        self._tools: list = []

    def _new_client(self) -> MCPClient:
        auth = SigV4HttpxAuth(self.settings.aws_region) if self.settings.mcp_auth == "sigv4" else None
        return MCPClient(url=self.settings.mcp_url, auth_provider=auth, startup_timeout=15)

    def tools(self) -> list:
        if not self.settings.mcp_url:
            return []
        with self._lock:
            if self._client is None:
                client = self._new_client()
                try:
                    client.start()
                    self._tools = list(client.list_tools_sync())
                    self._client = client
                    logger.info("MCP connected tools=%s", [t.tool_name for t in self._tools])
                except Exception:
                    logger.exception("MCP server unavailable; continuing without tools")
                    try:
                        client.stop(None, None, None)
                    except Exception:
                        pass
                    return []
            return self._tools

    def healthy(self) -> bool:
        return self._client is not None or not self.settings.mcp_url


def make_agent_factory(settings: Settings, registry: ToolRegistry):
    def make_agent(context_id: str) -> Agent:
        return Agent(
            name=settings.agent_name,
            description="Customer-support agent: order status, refunds, policy questions.",
            model=build_model(settings),
            system_prompt=SYSTEM_PROMPT,
            tools=registry.tools(),
            hooks=[ToolPolicyHook(settings), GuardrailMetricsHook(settings)],
            callback_handler=None,
            trace_attributes={"a2a.context_id": context_id, "app.version": settings.app_version},
        )

    return make_agent
