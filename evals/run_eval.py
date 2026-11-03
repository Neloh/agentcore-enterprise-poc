"""Evaluation gate: run golden cases against an A2A agent and fail the build below a threshold.

Targets
  --url http://127.0.0.1:9000/                      local container (no auth)
  --runtime-arn ARN --endpoint staging              AgentCore Runtime (SigV4)

Checks are deterministic (expected / forbidden substrings per case), so the gate
is cheap and repeatable. For LLM-as-judge quality scores (helpfulness, goal
success), add AgentCore Evaluations on top - see README "Evaluations".
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

HERE = Path(__file__).parent


def build_client(args) -> tuple[httpx.Client, str]:
    if args.url:
        return httpx.Client(timeout=args.timeout), args.url
    sys.path.insert(0, str(HERE.parent / "services" / "agent"))
    from app.sigv4_auth import SigV4HttpxAuth  # noqa: E402

    region = args.runtime_arn.split(":")[3]
    url = (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
           f"{quote(args.runtime_arn, safe='')}/invocations/?qualifier={args.endpoint}")
    return httpx.Client(timeout=args.timeout, auth=SigV4HttpxAuth(region)), url


def send(client: httpx.Client, url: str, prompt: str) -> str:
    session_id = str(uuid.uuid4())  # AgentCore needs >= 33 chars; uuid4 is 36
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                               "parts": [{"kind": "text", "text": prompt}]}},
    }
    resp = client.post(url, json=body, headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    result = data["result"]
    texts = [p.get("text", "") for a in result.get("artifacts", []) for p in a.get("parts", [])]
    if not texts:  # result may be a Message rather than a Task
        texts = [p.get("text", "") for p in result.get("parts", [])]
    return "\n".join(texts)


def grade(case: dict, answer: str) -> tuple[bool, str]:
    low = answer.lower()
    if case.get("expect_any") and not any(s.lower() in low for s in case["expect_any"]):
        return False, f"none of {case['expect_any']} in answer"
    for forbidden in case.get("expect_none", []):
        if forbidden.lower() in low:
            return False, f"forbidden text {forbidden!r} in answer"
    return True, "ok"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--runtime-arn")
    p.add_argument("--endpoint", default="staging")
    p.add_argument("--cases", default=str(HERE / "cases.json"))
    p.add_argument("--threshold", type=float, default=1.0, help="minimum pass rate (0-1)")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--report", default="eval-report.json")
    args = p.parse_args(argv)

    cases = json.loads(Path(args.cases).read_text())
    client, url = build_client(args)
    results = []
    for case in cases:
        try:
            answer = send(client, url, case["prompt"])
            ok, why = grade(case, answer)
        except Exception as exc:  # a transport failure is a failed case, not a crash
            answer, ok, why = "", False, f"error: {exc}"
        results.append({"id": case["id"], "category": case.get("category"), "passed": ok,
                        "reason": why, "answer": answer[:500]})
        print(f"[{'PASS' if ok else 'FAIL'}] {case['id']}: {why}")

    rate = sum(r["passed"] for r in results) / len(results)
    Path(args.report).write_text(json.dumps({"pass_rate": rate, "threshold": args.threshold,
                                             "results": results}, indent=2))
    print(f"pass rate {rate:.0%} (threshold {args.threshold:.0%})")
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
