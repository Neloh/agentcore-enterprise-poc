"""AgentCore Runtime release tooling: deploy versions, promote endpoints, bake, roll back.

Release model
-------------
* Every UpdateAgentRuntime call creates a new immutable runtime *version*.
* Named *endpoints* ("staging", "live") point at a specific version. Clients
  call qualifier=live; the DEFAULT endpoint always tracks the newest version,
  so production callers must never use DEFAULT.
* Promotion = UpdateAgentRuntimeEndpoint(endpoint, version).
* Rollback  = UpdateAgentRuntimeEndpoint(endpoint, previous_version). No image
  rebuild, no redeploy - it is a pointer move, typically done in seconds.
* The previous live version is stored in SSM Parameter Store so a human can
  roll back later with one command, independent of the pipeline.

Usage (see buildspecs/*.yml for the pipeline wiring):
  python deploy/agentcore.py deploy   --name poc_agent --image URI --role ARN --protocol A2A --env K=V ...
  python deploy/agentcore.py promote  --name poc_agent --endpoint live --version 7
  python deploy/agentcore.py bake     --name poc_agent --endpoint live --alarms a,b --minutes 10
  python deploy/agentcore.py rollback --name poc_agent --endpoint live [--version 6]
  python deploy/agentcore.py status   --name poc_agent
  python deploy/agentcore.py url      --name poc_mcp_tools --endpoint live
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any
from urllib.parse import quote

import boto3

log = logging.getLogger("agentcore-deploy")

TERMINAL_FAIL = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}


# --------------------------------------------------------------------------- helpers
def runtime_invocation_url(runtime_arn: str, endpoint: str | None = None) -> str:
    region = runtime_arn.split(":")[3]
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(runtime_arn, safe='')}/invocations"
    return f"{url}?qualifier={endpoint}" if endpoint else url


def ssm_param_name(runtime_name: str, endpoint: str) -> str:
    return f"/agentcore-poc/{runtime_name}/{endpoint}/previous-version"


def parse_env(pairs: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--env expects KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def find_runtime(ctl, name: str) -> dict | None:
    token = None
    while True:
        page = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for rt in page.get("agentRuntimes", []):
            if rt["agentRuntimeName"] == name:
                return rt
        token = page.get("nextToken")
        if not token:
            return None


def require_runtime(ctl, name: str) -> dict:
    rt = find_runtime(ctl, name)
    if rt is None:
        raise SystemExit(f"runtime {name!r} not found")
    return rt


def wait_runtime(ctl, runtime_id: str, timeout: int = 900, poll: int = 10) -> dict:
    deadline = time.time() + timeout
    while True:
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            return rt
        if status in TERMINAL_FAIL:
            raise RuntimeError(f"runtime {runtime_id} {status}: {rt.get('failureReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"runtime {runtime_id} still {status}")
        time.sleep(poll)


def get_endpoint(ctl, runtime_id: str, endpoint: str) -> dict | None:
    try:
        return ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint)
    except ctl.exceptions.ResourceNotFoundException:
        return None


def wait_endpoint(ctl, runtime_id: str, endpoint: str, version: str, timeout: int = 600, poll: int = 5) -> dict:
    deadline = time.time() + timeout
    while True:
        ep = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint)
        if ep["status"] == "READY" and str(ep.get("liveVersion")) == str(version):
            return ep
        if ep["status"] in TERMINAL_FAIL:
            raise RuntimeError(f"endpoint {endpoint} {ep['status']}: {ep.get('failureReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"endpoint {endpoint} status={ep['status']} live={ep.get('liveVersion')}")
        time.sleep(poll)


# --------------------------------------------------------------------------- operations
def deploy(ctl, *, name: str, image: str, role_arn: str, protocol: str, env: dict[str, str],
           description: str = "") -> dict[str, str]:
    """Create the runtime if needed, then UpdateAgentRuntime -> new version.

    Update is always called because metadataConfiguration (MMDSv2, mandatory
    since 2026-06-30) is only accepted by UpdateAgentRuntime.
    """
    common = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": protocol},
        "environmentVariables": env,
        "description": description[:1200],
    }
    existing = find_runtime(ctl, name)
    if existing is None:
        log.info("creating runtime %s", name)
        created = ctl.create_agent_runtime(agentRuntimeName=name, **common)
        runtime_id = created["agentRuntimeId"]
        wait_runtime(ctl, runtime_id)
    else:
        runtime_id = existing["agentRuntimeId"]
        wait_runtime(ctl, runtime_id)  # never stack updates on an in-flight one

    updated = ctl.update_agent_runtime(
        agentRuntimeId=runtime_id, metadataConfiguration={"requireMMDSV2": True}, **common
    )
    rt = wait_runtime(ctl, runtime_id)
    result = {
        "name": name,
        "runtimeId": runtime_id,
        "runtimeArn": rt["agentRuntimeArn"],
        "version": str(updated["agentRuntimeVersion"]),
    }
    log.info("deployed %s", result)
    return result


def promote(ctl, ssm, *, name: str, endpoint: str, version: str) -> dict[str, str | None]:
    rt = require_runtime(ctl, name)
    runtime_id = rt["agentRuntimeId"]
    current = get_endpoint(ctl, runtime_id, endpoint)
    previous = str(current["liveVersion"]) if current and current.get("liveVersion") else None

    if previous == str(version):
        log.info("%s/%s already on version %s", name, endpoint, version)
    elif current is None:
        log.info("creating endpoint %s/%s -> v%s", name, endpoint, version)
        ctl.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=endpoint, agentRuntimeVersion=str(version))
    else:
        log.info("moving endpoint %s/%s v%s -> v%s", name, endpoint, previous, version)
        ctl.update_agent_runtime_endpoint(
            agentRuntimeId=runtime_id, endpointName=endpoint, agentRuntimeVersion=str(version)
        )
    wait_endpoint(ctl, runtime_id, endpoint, str(version))

    if previous and previous != str(version):
        ssm.put_parameter(Name=ssm_param_name(name, endpoint), Value=previous, Type="String", Overwrite=True)
    return {"name": name, "endpoint": endpoint, "version": str(version), "previous": previous,
            "runtimeArn": rt["agentRuntimeArn"]}


def rollback(ctl, ssm, *, name: str, endpoint: str, version: str | None = None) -> dict[str, str | None]:
    if version is None:
        version = ssm.get_parameter(Name=ssm_param_name(name, endpoint))["Parameter"]["Value"]
    log.warning("ROLLBACK %s/%s -> v%s", name, endpoint, version)
    return promote(ctl, ssm, name=name, endpoint=endpoint, version=version)


def alarms_in_alarm(cw, alarm_names: list[str]) -> list[str]:
    if not alarm_names:
        return []
    firing: list[str] = []
    resp = cw.describe_alarms(AlarmNames=alarm_names, AlarmTypes=["MetricAlarm", "CompositeAlarm"])
    for alarm in resp.get("MetricAlarms", []) + resp.get("CompositeAlarms", []):
        if alarm["StateValue"] == "ALARM":
            firing.append(alarm["AlarmName"])
    return firing


def bake(ctl, ssm, cw, *, targets: list[tuple[str, str, str | None]], alarm_names: list[str],
         minutes: float, poll: int = 30, sleep=time.sleep) -> bool:
    """Watch alarms for `minutes`. On any ALARM, roll every target back.

    targets: (runtime_name, endpoint, previous_version). Rolled back in order,
    so list the caller (agent) before its dependency (MCP tools).
    Returns True when the bake passed.
    """
    deadline = time.time() + minutes * 60
    while True:
        firing = alarms_in_alarm(cw, alarm_names)
        if firing:
            log.error("alarms firing during bake: %s", firing)
            for name, endpoint, previous in targets:
                if previous:
                    rollback(ctl, ssm, name=name, endpoint=endpoint, version=previous)
                else:
                    log.error("no previous version for %s/%s (first release) - cannot roll back", name, endpoint)
            return False
        if time.time() >= deadline:
            log.info("bake passed (%s min, alarms=%s)", minutes, alarm_names)
            return True
        sleep(poll)


def status(ctl, *, name: str) -> dict[str, Any]:
    rt = require_runtime(ctl, name)
    rid = rt["agentRuntimeId"]
    eps = ctl.list_agent_runtime_endpoints(agentRuntimeId=rid).get("runtimeEndpoints", [])
    versions = ctl.list_agent_runtime_versions(agentRuntimeId=rid).get("agentRuntimes", [])
    return {
        "runtimeArn": rt["agentRuntimeArn"],
        "endpoints": {e["name"]: {"live": e.get("liveVersion"), "target": e.get("targetVersion"),
                                  "status": e["status"]} for e in eps},
        "versions": sorted((v["agentRuntimeVersion"] for v in versions), key=lambda v: int(v)),
    }


# --------------------------------------------------------------------------- CLI
def _write_output(path: str | None, data: dict) -> None:
    print(json.dumps(data, indent=2, default=str))
    if path:
        with open(path, "w") as fh:
            json.dump(data, fh, default=str)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy")
    d.add_argument("--name", required=True)
    d.add_argument("--image", required=True)
    d.add_argument("--role", required=True)
    d.add_argument("--protocol", choices=["A2A", "MCP", "HTTP"], required=True)
    d.add_argument("--env", action="append")
    d.add_argument("--description", default="")
    d.add_argument("--out")

    pr = sub.add_parser("promote")
    pr.add_argument("--name", required=True)
    pr.add_argument("--endpoint", required=True)
    pr.add_argument("--version", required=True)
    pr.add_argument("--out")

    rb = sub.add_parser("rollback")
    rb.add_argument("--name", required=True)
    rb.add_argument("--endpoint", default="live")
    rb.add_argument("--version", help="defaults to the version recorded in SSM at last promotion")

    bk = sub.add_parser("bake")
    bk.add_argument("--target", action="append", required=True,
                    help="name:endpoint:previous_version (previous may be empty); agent first")
    bk.add_argument("--alarms", required=True, help="comma-separated alarm names")
    bk.add_argument("--minutes", type=float, default=10)
    bk.add_argument("--poll", type=int, default=30)

    st = sub.add_parser("status")
    st.add_argument("--name", required=True)

    u = sub.add_parser("url")
    u.add_argument("--name", required=True)
    u.add_argument("--endpoint")

    args = p.parse_args(argv)
    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    ssm = boto3.client("ssm", region_name=args.region)

    if args.cmd == "deploy":
        _write_output(args.out, deploy(ctl, name=args.name, image=args.image, role_arn=args.role,
                                       protocol=args.protocol, env=parse_env(args.env),
                                       description=args.description))
    elif args.cmd == "promote":
        _write_output(args.out, promote(ctl, ssm, name=args.name, endpoint=args.endpoint, version=args.version))
    elif args.cmd == "rollback":
        _write_output(None, rollback(ctl, ssm, name=args.name, endpoint=args.endpoint, version=args.version))
    elif args.cmd == "bake":
        cw = boto3.client("cloudwatch", region_name=args.region)
        targets = []
        for t in args.target:
            name, endpoint, previous = (t.split(":") + ["", ""])[:3]
            targets.append((name, endpoint, previous or None))
        ok = bake(ctl, ssm, cw, targets=targets, alarm_names=[a for a in args.alarms.split(",") if a],
                  minutes=args.minutes, poll=args.poll)
        return 0 if ok else 1
    elif args.cmd == "status":
        _write_output(None, status(ctl, name=args.name))
    elif args.cmd == "url":
        rt = require_runtime(ctl, args.name)
        print(runtime_invocation_url(rt["agentRuntimeArn"], args.endpoint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
