"""SigV4 request signing for agent -> MCP runtime calls.

When the MCP server is hosted on AgentCore Runtime with the default IAM inbound
authorizer, every request must be SigV4-signed for service "bedrock-agentcore".
The agent's execution role needs bedrock-agentcore:InvokeAgentRuntime on the
MCP runtime ARN (granted in infra/foundation.yaml).
"""

from __future__ import annotations

from collections.abc import Generator

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

_SIGNED_HEADERS = ("content-type", "accept")


class SigV4HttpxAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore", session: boto3.Session | None = None):
        self._region = region
        self._service = service
        self._session = session or boto3.Session()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available for SigV4 signing")
        headers = {k: v for k, v in request.headers.items() if k.lower() in _SIGNED_HEADERS}
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), self._service, self._region).add_auth(aws_request)
        for name, value in aws_request.headers.items():
            request.headers[name] = value
        yield request
