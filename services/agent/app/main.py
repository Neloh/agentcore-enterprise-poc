"""A2A server entrypoint, hosted on AgentCore Runtime with serverProtocol=A2A.

AgentCore A2A contract: JSON-RPC at "/" on port 9000, agent card at
/.well-known/agent-card.json, health at /ping. bedrock_agentcore's
build_a2a_app wires all three and propagates AgentCore session/trace context.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from bedrock_agentcore.runtime.a2a import build_a2a_app
from bedrock_agentcore.runtime.models import PingStatus
from strands.multiagent.a2a.executor import StrandsA2AExecutor

from .agent_factory import ToolRegistry, make_agent_factory
from .config import Settings

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

settings = Settings()
registry = ToolRegistry(settings)

AGENT_CARD = AgentCard(
    name=settings.agent_name,
    description="Enterprise customer-support agent (Strands on Amazon Bedrock AgentCore).",
    url=settings.public_url,
    version=settings.app_version,
    capabilities=AgentCapabilities(streaming=True),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[
        AgentSkill(
            id="order_support",
            name="Order support",
            description="Look up order status and calculate refund eligibility.",
            tags=["orders", "refunds"],
            examples=["Where is order ORD-1001?", "Can I get a refund of 50 on ORD-1002?"],
        ),
        AgentSkill(
            id="policy_qa",
            name="Policy questions",
            description="Answer returns, shipping and warranty policy questions.",
            tags=["policy"],
        ),
    ],
)


def ping() -> PingStatus:
    # Stay Healthy even if MCP is down: the agent degrades to no-tools mode and
    # the error shows up in metrics/alarms rather than as a crash loop.
    return PingStatus.HEALTHY


executor = StrandsA2AExecutor(agent_factory=make_agent_factory(settings, registry))
app = build_a2a_app(executor, AGENT_CARD, runtime_url=settings.public_url, ping_handler=ping)


if __name__ == "__main__":
    # Loopback by default; the image sets BIND_HOST=0.0.0.0 for AgentCore.
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "127.0.0.1"), port=int(os.environ.get("A2A_PORT", "9000")))
