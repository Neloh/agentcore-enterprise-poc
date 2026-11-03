# AgentCore Enterprise PoC — Strands A2A agent + MCP tools + Guardrails + CI/CD + rollback

A small, runnable reference that implements the parts of the
[Enterprise Agentic AI Platform guidance](https://github.com/aws-solutions-library-samples/guidance-for-enterprise-agentic-ai-platform-on-aws)
customers ask about most: **CI/CD with evaluation gates and approval, monitoring,
hosting an agent over A2A, guardrails, MCP tool support, and rollback**, all on
Amazon Bedrock AgentCore Runtime, using Strands Agents.

This is the **single-account tier**. `staging` and `live` are two endpoints of the
same runtime. The guidance's enterprise tier puts each stage in its own account.
The release mechanics in this repo stay the same in that setup. Only where each
stage deploys changes.

## Architecture

```
                 A2A (JSON-RPC, SigV4)            MCP (streamable HTTP, SigV4)
 caller ──► AgentCore Runtime: poc_agent ─────────► AgentCore Runtime: poc_mcp_tools
            endpoints: staging | live               endpoints: staging | live
            ┌──────────────────────────────┐        ┌───────────────────────────┐
            │ Strands Agent (per context)  │        │ MCPServer (stateless)     │
            │  ├ BedrockModel + Guardrail  │──► Amazon Bedrock (Claude Haiku 4.5)
            │  ├ ToolPolicyHook (allowlist,│        │  get_order_status         │
            │  │   budget, refund approval)│        │  calculate_refund         │
            │  ├ GuardrailMetricsHook (EMF)│        │  search_policy_kb         │
            │  └ MCPClient (shared)        │        │  + input validation       │
            └──────────────────────────────┘        └───────────────────────────┘
                 │ ADOT (opentelemetry-instrument)        │ ADOT
                 ▼                                        ▼
   CloudWatch: AWS/Bedrock-AgentCore metrics, AgentCorePoc custom metrics, logs, X-Ray spans
   ──► alarms ──► composite "<project>-rollback" ──► watched by pipeline bake ──► auto rollback

 CodePipeline V2:  Source ─► Build&Test ─► Staging (deploy + eval gate) ─► Manual approval ─► Production (promote + bake + rollback)
```

| Path | What it is |
|---|---|
| `services/agent/` | Strands agent served over A2A with `bedrock_agentcore.runtime.a2a.build_a2a_app` (port 9000, `/ping`, agent card) |
| `services/mcp_tools/` | MCP tool server (`mcp` 2.x `MCPServer`, stateless streamable HTTP on port 8000 at `/mcp`) |
| `services/agent/app/policy.py` | Tool-policy guardrail + EMF custom metrics |
| `services/agent/app/stub_model.py` | Offline stand-in for Bedrock, used for local and CI tests only |
| `deploy/agentcore.py` | Release tool: deploy version, promote endpoint, bake, roll back, status |
| `evals/` | Eval gate (golden cases, pass-rate threshold) |
| `infra/foundation.yaml` | ECR, Bedrock Guardrail + version, runtime execution role, SNS |
| `infra/monitoring.yaml` | Alarms, composite rollback alarm, dashboard |
| `infra/pipeline.yaml` | CodePipeline + 3 ARM64 CodeBuild projects |
| `buildspecs/` | build / staging / production stages |

Pinned versions (verified 2026-11-03): strands-agents 1.63.0, bedrock-agentcore 1.25.2,
mcp 2.3.0, a2a-sdk 0.3.26, aws-opentelemetry-distro 0.22.0.

## 1. Run locally (Colima / Docker, no AWS needed)

```bash
colima start --arch aarch64 --cpu 4 --memory 8      # AgentCore needs linux/arm64 images
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests                  # 34 unit tests
docker compose up --build -d                          # agent on 127.0.0.1:9000, MCP internal only
curl -s 127.0.0.1:9000/ping
curl -s 127.0.0.1:9000/.well-known/agent-card.json
.venv/bin/python evals/run_eval.py --url http://127.0.0.1:9000/
docker compose down
```

By default this uses `MODEL_PROVIDER=stub`, a deterministic model that drives the
real Strands event loop, hooks, MCP calls and A2A server, so the full path works offline.

**Local run against real Bedrock and Guardrails:** deploy `infra/foundation.yaml` first
to get a guardrail, then set `MODEL_PROVIDER=bedrock GUARDRAIL_ID=... GUARDRAIL_VERSION=...`.
Pass credentials into the agent container (for example with a compose override that sets
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` from your shell).

If `docker build` fails with `403 Forbidden` from `public.ecr.aws`, run `docker logout public.ecr.aws`.
An expired cached login breaks anonymous pulls.

## 2. Deploy to AWS

Prerequisites:
- Region with AgentCore Runtime.
- Bedrock model access for Claude Haiku 4.5.
- CloudWatch Transaction Search enabled once per account and region (see Observability below).
- A CodeConnections connection to your Git provider in `AVAILABLE` state.
- This directory pushed to that repository.

```bash
AWS_REGION=us-east-1 CONNECTION_ARN=arn:aws:codeconnections:... REPO=owner/repo \
ALERT_EMAIL=you@example.com ./scripts/bootstrap.sh
```

Pushing to the branch then runs the pipeline:

1. **BuildAndTest:** unit tests, cfn-lint, native ARM64 `docker build`, offline
   integration test of the exact images (eval suite with the stub model), push to ECR
   (immutable tags `<sha>-<build>`).
2. **Staging:** new runtime versions for MCP and agent, `staging` endpoints moved to them,
   alarms and dashboard (re)deployed, then the **eval gate** runs against `poc_agent::staging`
   with the real model and real guardrail. The stage fails below `EvalThreshold`.
3. **Approval:** manual, notified through SNS.
4. **Production:** MCP `live` endpoint promoted. The same agent image gets a new version
   whose only change is `MCP_URL` pointing at `MCP::live`, and the agent `live` endpoint is
   promoted. A smoke eval runs on `live`, then the **bake** watches the composite alarm for
   `BakeMinutes`. Any failure moves both endpoints back to their previous versions.

## 3. Rollback

On AgentCore, every `UpdateAgentRuntime` creates an immutable **version**. Callers use a
named **endpoint** (`qualifier=live`), and the endpoint points at one version. So a
rollback is just moving that pointer (`UpdateAgentRuntimeEndpoint`), with no rebuild
and no redeploy.

```bash
python deploy/agentcore.py status   --name poc_agent                    # endpoints and versions
python deploy/agentcore.py rollback --name poc_agent --endpoint live    # to the version stored in SSM at last promotion
python deploy/agentcore.py rollback --name poc_agent --endpoint live --version 4
```

- **Automatic:** the bake step rolls back if `<project>-rollback` fires (agent 5xx, agent
  p90 latency, MCP 5xx, model call errors), or if the live smoke eval fails.
- **Manual:** use the commands above at any time. The previous version is stored at
  `/agentcore-poc/<runtime>/<endpoint>/previous-version` in SSM.
- Never point production callers at `DEFAULT`. It always tracks the newest version, so
  deploying a version would bypass the gate.
- Rollback order: agent first, then MCP. MCP tool changes must be backward compatible
  (additive), because the previous agent version keeps calling `MCP::live`.

## 4. Guardrails, in layers

| Layer | Where | What it stops |
|---|---|---|
| Amazon Bedrock Guardrails | `infra/foundation.yaml`, applied by `BedrockModel(guardrail_id, guardrail_version, guardrail_redact_output=True)` | Harmful content, prompt attacks (input), denied topic (investment advice), PII (email and phone anonymized; cards and AWS keys blocked) |
| Tool policy | `ToolPolicyHook` (`BeforeToolCallEvent.cancel_tool`) | Tools not on the allowlist, more than `MAX_TOOL_CALLS` per request, refunds above `REFUND_APPROVAL_LIMIT` (escalated to a human) |
| Tool-side validation | MCP server | Malformed IDs and amounts, server-side refund ceiling. The tool server never trusts the agent |
| Identity and network | AgentCore IAM inbound auth (SigV4) on both runtimes. The execution role can only invoke the MCP runtime | Unauthenticated callers. Agent-to-tool calls are signed |

Every intervention or denial emits a metric (`GuardrailInterventions`, `ToolCallsDenied`,
`ToolCallsAllowed`, `ModelCallErrors`). These feed the dashboard and alarms.

At scale, the managed versions of the tool-policy layer are **AgentCore Gateway** (one
MCP front door for many tools, with OAuth through AgentCore Identity) and
**AgentCore Policy** (Cedar rules that Gateway enforces on each tool call). This PoC calls
the MCP runtime directly to keep the moving parts visible. Swapping in Gateway changes
`MCP_URL` and the auth provider, not the agent code.

## 5. Observability

- **Service metrics** (automatic): `AWS/Bedrock-AgentCore` `Invocations`, `SystemErrors`,
  `UserErrors`, `Throttles`, `Latency`, with dimensions
  `Resource=<runtime ARN>, Operation=InvokeAgentRuntime, Name=<runtime>::<endpoint>`.
- **Traces and spans:** both images start with `opentelemetry-instrument` (ADOT 0.22). Strands
  emits GenAI spans for the agent loop, model calls and tool calls. View them under CloudWatch >
  GenAI Observability > Bedrock AgentCore. This **requires CloudWatch Transaction Search**,
  once per account and region:
  `aws xray update-trace-segment-destination --destination CloudWatchLogs` (plus the logs resource policy from the AgentCore docs).
- **Custom metrics:** EMF lines on stdout, extracted by CloudWatch Logs into the `AgentCorePoc` namespace.
- **Dashboard:** `<project>-operations`, from `infra/monitoring.yaml`.
- **Session correlation:** send `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` (33+ characters) on each call.

## 6. Evaluations

`evals/run_eval.py` is a deterministic gate: golden prompts with expected or forbidden text,
covering tool use, policy denial and guardrail blocking. It is cheap, repeatable and blocks
the pipeline. For quality scoring (helpfulness, goal success), add **AgentCore Evaluations**
built-in evaluators, as the guidance does, as a second step in the Staging stage. Run it on
the same `staging` endpoint traffic and fail the stage below a score threshold.

## 7. Cost and cleanup

Everything is pay-per-use. Runtimes scale to zero, and the only standing charges are the
ECR storage, CloudWatch alarms and dashboard, and the S3 artifact bucket. The guidance
estimates about $5/month at roughly 1,000 invocations. Use the AWS Pricing Calculator
for your own volumes.

```bash
# AgentCore runtimes are created by the pipeline, not CloudFormation, so delete them explicitly
for n in poc_agent poc_mcp_tools; do
  id=$(aws bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?agentRuntimeName=='$n'].agentRuntimeId" --output text)
  [ -n "$id" ] && aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$id"
done
aws cloudformation delete-stack --stack-name agentcore-poc-pipeline
aws cloudformation delete-stack --stack-name agentcore-poc-monitoring
# Empty both ECR repositories first; repositories that still contain images make the delete fail
aws cloudformation delete-stack --stack-name agentcore-poc-foundation
aws ssm delete-parameters --names /agentcore-poc/poc_agent/live/previous-version /agentcore-poc/poc_mcp_tools/live/previous-version
```

## Known limits of this PoC

- Single account. Use the guidance's multi-account tier for production isolation.
- Inbound auth is IAM (SigV4). For end users, configure a JWT authorizer on the agent runtime (Cognito or your IdP).
- No AgentCore Memory. Conversation state is per A2A context, in memory.
- Region note: in the CloudFormation resource registry bundled with cfn-lint 1.59,
  `AWS::BedrockAgentCore::Memory` is not listed for ap-south-2 (Hyderabad). Runtime,
  Gateway, Policy and Evaluator are listed for both ap-south-1 and ap-south-2. Confirm
  current availability in the AgentCore Regions documentation before committing to a region.
