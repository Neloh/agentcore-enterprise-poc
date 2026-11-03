"""Enterprise MCP tool server, hosted on AgentCore Runtime with serverProtocol=MCP.

AgentCore MCP contract: stateless streamable-HTTP on port 8000 at path /mcp.

Tools are deliberately deterministic (in-memory data) so the PoC runs without
any backend. Replace the bodies with calls to your systems of record.

Guardrail layer (server side): every tool validates its own inputs. The agent
also enforces a tool policy, but the tool server must never trust its caller.
"""

from __future__ import annotations

import logging
import os
import re

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mcp_tools")

ORDER_ID_RE = re.compile(r"^ORD-\d{4,8}$")
MAX_REFUND = 500.0  # hard ceiling enforced by the tool itself

_ORDERS = {
    "ORD-1001": {"status": "SHIPPED", "carrier": "UPS", "eta": "2026-11-05", "total": 129.99},
    "ORD-1002": {"status": "PROCESSING", "carrier": None, "eta": "2026-11-09", "total": 54.00},
    "ORD-1003": {"status": "DELIVERED", "carrier": "DHL", "eta": "2026-10-30", "total": 899.00},
}

_KB = {
    "returns": "Items can be returned within 30 days of delivery in original packaging.",
    "shipping": "Standard shipping takes 3-5 business days; express takes 1-2.",
    "warranty": "Electronics carry a 12-month limited manufacturer warranty.",
}

mcp = MCPServer(
    name="enterprise-tools",
    instructions="Order lookup, refund calculation and policy knowledge base tools.",
    version=os.environ.get("APP_VERSION", "0.0.0"),
)


def _validate_order_id(order_id: str) -> str:
    order_id = (order_id or "").strip().upper()
    if not ORDER_ID_RE.match(order_id):
        raise ValueError("order_id must look like ORD-1234")
    return order_id


@mcp.tool()
def get_order_status(order_id: str) -> dict:
    """Return shipping status, carrier and ETA for an order (e.g. ORD-1001)."""
    order_id = _validate_order_id(order_id)
    order = _ORDERS.get(order_id)
    if order is None:
        return {"order_id": order_id, "found": False}
    return {"order_id": order_id, "found": True, **order}


@mcp.tool()
def calculate_refund(order_id: str, amount: float) -> dict:
    """Calculate an eligible refund for an order. Does not issue the refund."""
    order_id = _validate_order_id(order_id)
    if amount <= 0:
        raise ValueError("amount must be positive")
    order = _ORDERS.get(order_id)
    if order is None:
        return {"order_id": order_id, "eligible": False, "reason": "order not found"}
    eligible = min(amount, order["total"], MAX_REFUND)
    return {
        "order_id": order_id,
        "eligible": True,
        "requested": amount,
        "eligible_amount": round(eligible, 2),
        "capped": eligible < amount,
    }


@mcp.tool()
def search_policy_kb(topic: str) -> dict:
    """Look up company policy text. Topics: returns, shipping, warranty."""
    topic = (topic or "").strip().lower()[:64]
    hit = next((v for k, v in _KB.items() if k in topic), None)
    return {"topic": topic, "found": hit is not None, "text": hit or ""}


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection.

    Locally (docker compose) set MCP_ALLOWED_HOSTS to the service names that may
    appear in the Host header. On AgentCore Runtime the service is reached only
    through the AgentCore data plane, which authenticates every request (SigV4
    or JWT inbound authorizer), so header-based protection is left off there.
    """
    allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed)


def build_app():
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # AgentCore requires stateless MCP servers
        json_response=True,
        transport_security=_transport_security(),
    )


app = build_app()

if __name__ == "__main__":
    # Default to loopback; the container image sets BIND_HOST=0.0.0.0 because
    # AgentCore must reach the port inside the microVM. Local docker runs
    # publish the port to 127.0.0.1 only (see docker-compose.yml).
    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
