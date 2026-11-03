"""Release/rollback logic against an in-memory fake of the AgentCore control plane."""

import pytest

import agentcore as ac

ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/poc_agent-abc"


class NotFound(Exception):
    pass


class FakeCtl:
    class exceptions:
        ResourceNotFoundException = NotFound

    def __init__(self):
        self.runtime = None
        self.endpoints = {}
        self.calls = []

    def list_agent_runtimes(self, **_):
        return {"agentRuntimes": [self.runtime] if self.runtime else []}

    def create_agent_runtime(self, agentRuntimeName, **kw):
        assert "metadataConfiguration" not in kw  # not accepted by CreateAgentRuntime
        self.runtime = {"agentRuntimeName": agentRuntimeName, "agentRuntimeId": "rt-1",
                        "agentRuntimeArn": ARN, "agentRuntimeVersion": "1", "status": "READY"}
        self.calls.append(("create", kw))
        return self.runtime

    def update_agent_runtime(self, agentRuntimeId, **kw):
        self.runtime["agentRuntimeVersion"] = str(int(self.runtime["agentRuntimeVersion"]) + 1)
        self.calls.append(("update", kw))
        return {"agentRuntimeVersion": self.runtime["agentRuntimeVersion"]}

    def get_agent_runtime(self, agentRuntimeId):
        return self.runtime

    def get_agent_runtime_endpoint(self, agentRuntimeId, endpointName):
        if endpointName not in self.endpoints:
            raise NotFound()
        return {"status": "READY", "liveVersion": self.endpoints[endpointName]}

    def create_agent_runtime_endpoint(self, agentRuntimeId, name, agentRuntimeVersion):
        self.endpoints[name] = agentRuntimeVersion

    def update_agent_runtime_endpoint(self, agentRuntimeId, endpointName, agentRuntimeVersion):
        self.endpoints[endpointName] = agentRuntimeVersion


class FakeSSM:
    def __init__(self):
        self.params = {}

    def put_parameter(self, Name, Value, **_):
        self.params[Name] = Value

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.params[Name]}}


class FakeCW:
    def __init__(self, states):
        self.states = list(states)

    def describe_alarms(self, AlarmNames, **_):
        state = self.states.pop(0) if self.states else "OK"
        return {"MetricAlarms": [{"AlarmName": n, "StateValue": state} for n in AlarmNames]}


@pytest.fixture
def ctl():
    return FakeCtl()


def _deploy(ctl, image="img:1"):
    return ac.deploy(ctl, name="poc_agent", image=image, role_arn="arn:role", protocol="A2A", env={"A": "1"})


def test_first_deploy_creates_then_updates_with_mmdsv2(ctl):
    out = _deploy(ctl)
    assert [c[0] for c in ctl.calls] == ["create", "update"]
    assert ctl.calls[1][1]["metadataConfiguration"] == {"requireMMDSV2": True}
    assert ctl.calls[1][1]["protocolConfiguration"] == {"serverProtocol": "A2A"}
    assert out["version"] == "2" and out["runtimeArn"] == ARN


def test_redeploy_only_updates(ctl):
    _deploy(ctl)
    out = _deploy(ctl, "img:2")
    assert [c[0] for c in ctl.calls] == ["create", "update", "update"]
    assert out["version"] == "3"


def test_promote_records_previous_and_rollback_restores(ctl):
    ssm = FakeSSM()
    _deploy(ctl)
    first = ac.promote(ctl, ssm, name="poc_agent", endpoint="live", version="2")
    assert first["previous"] is None and ctl.endpoints["live"] == "2"

    _deploy(ctl, "img:2")
    second = ac.promote(ctl, ssm, name="poc_agent", endpoint="live", version="3")
    assert second["previous"] == "2"
    assert ssm.params[ac.ssm_param_name("poc_agent", "live")] == "2"

    ac.rollback(ctl, ssm, name="poc_agent", endpoint="live")
    assert ctl.endpoints["live"] == "2"


def test_bake_passes_when_alarms_ok(ctl):
    ok = ac.bake(ctl, FakeSSM(), FakeCW(["OK", "OK"]), targets=[("poc_agent", "live", "2")],
                 alarm_names=["a"], minutes=0, sleep=lambda _: None)
    assert ok


def test_bake_rolls_back_on_alarm(ctl):
    ssm = FakeSSM()
    _deploy(ctl)
    ac.promote(ctl, ssm, name="poc_agent", endpoint="live", version="2")
    _deploy(ctl, "img:2")
    ac.promote(ctl, ssm, name="poc_agent", endpoint="live", version="3")
    ok = ac.bake(ctl, ssm, FakeCW(["OK", "ALARM"]), targets=[("poc_agent", "live", "2")],
                 alarm_names=["agent-errors"], minutes=1, sleep=lambda _: None)
    assert not ok and ctl.endpoints["live"] == "2"


def test_invocation_url_encodes_arn():
    url = ac.runtime_invocation_url(ARN, "live")
    assert url.startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore")
    assert url.endswith("/invocations?qualifier=live")


def test_parse_env():
    assert ac.parse_env(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}
    with pytest.raises(ValueError):
        ac.parse_env(["bad"])
