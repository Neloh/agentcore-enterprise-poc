"""Runtime configuration, read once from environment variables.

Everything environment-specific (model, guardrail, MCP endpoint, policy limits)
is injected by the deploy step, so the same image is promoted unchanged from
staging to live. That is what makes endpoint-level rollback safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    agent_name: str = field(default_factory=lambda: os.environ.get("AGENT_NAME", "poc_agent"))
    app_version: str = field(default_factory=lambda: os.environ.get("APP_VERSION", "dev"))
    aws_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))

    # Model: "bedrock" in AWS, "stub" for offline local tests (no AWS creds needed).
    model_provider: str = field(default_factory=lambda: os.environ.get("MODEL_PROVIDER", "bedrock"))
    model_id: str = field(
        default_factory=lambda: os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    )

    # Amazon Bedrock Guardrails (applied to model input and output).
    guardrail_id: str | None = field(default_factory=lambda: os.environ.get("GUARDRAIL_ID") or None)
    guardrail_version: str | None = field(default_factory=lambda: os.environ.get("GUARDRAIL_VERSION") or None)

    # MCP tool server. Locally: http://mcp-tools:8000/mcp. On AgentCore: the MCP
    # runtime invocation URL (see deploy/agentcore.py:runtime_invocation_url).
    mcp_url: str | None = field(default_factory=lambda: os.environ.get("MCP_URL") or None)
    mcp_auth: str = field(default_factory=lambda: os.environ.get("MCP_AUTH", "none"))  # none | sigv4

    # Tool policy (application-level guardrail on tool use).
    allowed_tools: list[str] = field(
        default_factory=lambda: _csv("ALLOWED_TOOLS", "get_order_status,calculate_refund,search_policy_kb")
    )
    refund_approval_limit: float = field(
        default_factory=lambda: float(os.environ.get("REFUND_APPROVAL_LIMIT", "200"))
    )
    max_tool_calls: int = field(default_factory=lambda: int(os.environ.get("MAX_TOOL_CALLS", "8")))

    # Custom CloudWatch metrics (EMF on stdout; AgentCore ships stdout to CloudWatch Logs).
    metrics_namespace: str = field(default_factory=lambda: os.environ.get("METRICS_NAMESPACE", "AgentCorePoc"))

    # URL advertised in the A2A agent card. AGENTCORE_RUNTIME_URL (set by the
    # runtime) takes precedence inside bedrock_agentcore.runtime.a2a.
    public_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_URL", "http://127.0.0.1:9000/"))

    @property
    def guardrails_enabled(self) -> bool:
        return bool(self.guardrail_id and self.guardrail_version)
