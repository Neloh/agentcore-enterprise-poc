import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent"))
sys.path.insert(0, str(ROOT / "services" / "mcp_tools"))
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "evals"))


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("ALLOWED_TOOLS", "get_order_status,calculate_refund,search_policy_kb")
    monkeypatch.setenv("REFUND_APPROVAL_LIMIT", "200")
    monkeypatch.setenv("MAX_TOOL_CALLS", "3")
    from app.config import Settings

    return Settings()


def emf_records(captured: str) -> list[dict]:
    return [json.loads(line) for line in captured.splitlines() if line.startswith('{"_aws"')]
