import pytest
import server


def test_order_found():
    assert server.get_order_status("ord-1001")["status"] == "SHIPPED"


def test_order_not_found():
    assert server.get_order_status("ORD-9999") == {"order_id": "ORD-9999", "found": False}


@pytest.mark.parametrize("bad", ["", "1001", "ORD-1", "ORD-1001; DROP TABLE", "../etc/passwd"])
def test_order_id_validation(bad):
    with pytest.raises(ValueError):
        server.get_order_status(bad)


def test_refund_capped_by_order_total_and_ceiling():
    r = server.calculate_refund("ORD-1001", 1000)
    assert r["eligible_amount"] == 129.99 and r["capped"]
    r = server.calculate_refund("ORD-1003", 2000)
    assert r["eligible_amount"] == server.MAX_REFUND


def test_refund_rejects_non_positive():
    with pytest.raises(ValueError):
        server.calculate_refund("ORD-1001", 0)


def test_kb_lookup():
    assert server.search_policy_kb("Returns please")["found"]
    assert not server.search_policy_kb("crypto")["found"]


def test_transport_security(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    assert server._transport_security().enable_dns_rebinding_protection is False
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp-tools:8000, localhost:*")
    ts = server._transport_security()
    assert ts.enable_dns_rebinding_protection and ts.allowed_hosts == ["mcp-tools:8000", "localhost:*"]
