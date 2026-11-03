from types import SimpleNamespace

from app.policy import GuardrailMetricsHook, ToolPolicyHook, emit_metric
from conftest import emf_records


def test_allowlisted_tool_allowed(settings):
    assert ToolPolicyHook(settings).decide("get_order_status", {"order_id": "ORD-1001"}) is None


def test_unknown_tool_denied(settings):
    assert "not on the approved tool list" in ToolPolicyHook(settings).decide("delete_database", {})


def test_refund_over_limit_needs_approval(settings):
    hook = ToolPolicyHook(settings)
    assert hook.decide("calculate_refund", {"order_id": "ORD-1", "amount": 199}) is None
    assert "human approval" in hook.decide("calculate_refund", {"order_id": "ORD-1", "amount": 201})


def test_refund_amount_must_be_numeric(settings):
    assert "number" in ToolPolicyHook(settings).decide("calculate_refund", {"amount": "lots"})


def test_tool_budget_enforced_and_reset(settings, capsys):
    hook = ToolPolicyHook(settings)

    def call():
        ev = SimpleNamespace(tool_use={"name": "get_order_status", "input": {}}, cancel_tool=False)
        hook._check(ev)
        return ev.cancel_tool

    assert [call() for _ in range(3)] == [False, False, False]
    assert "budget" in call()
    hook._reset(None)
    assert call() is False
    names = [next(k for k in r if k in ("ToolCallsAllowed", "ToolCallsDenied")) for r in emf_records(capsys.readouterr().out)]
    assert names.count("ToolCallsDenied") == 1


def test_guardrail_intervention_metric(settings, capsys):
    hook = GuardrailMetricsHook(settings)
    hook._after_model(SimpleNamespace(exception=None, stop_response=SimpleNamespace(stop_reason="guardrail_intervened")))
    hook._after_model(SimpleNamespace(exception=None, stop_response=SimpleNamespace(stop_reason="end_turn")))
    hook._after_model(SimpleNamespace(exception=RuntimeError("x"), stop_response=None))
    recs = emf_records(capsys.readouterr().out)
    assert [r.get("GuardrailInterventions", r.get("ModelCallErrors")) for r in recs] == [1, 1]
    assert "ModelCallErrors" in recs[1]


def test_emf_shape(settings, capsys):
    emit_metric(settings, "X", 2, Tool="t")
    (rec,) = emf_records(capsys.readouterr().out)
    cwm = rec["_aws"]["CloudWatchMetrics"][0]
    assert cwm["Namespace"] == "AgentCorePoc"
    assert cwm["Dimensions"] == [["Service", "Tool"]]
    assert rec["X"] == 2 and rec["Service"] == "poc_agent" and rec["Tool"] == "t"
