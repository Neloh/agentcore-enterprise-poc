import boto3
import httpx
from botocore.credentials import Credentials

from app.sigv4_auth import SigV4HttpxAuth


class _Session(boto3.Session):
    def get_credentials(self):
        return Credentials("AKIDEXAMPLE", "secret", "token")


def test_sigv4_signs_request():
    auth = SigV4HttpxAuth("us-east-1", session=_Session(region_name="us-east-1"))
    req = httpx.Request("POST", "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/x/invocations",
                        content=b'{"a":1}', headers={"content-type": "application/json"})
    signed = next(auth.auth_flow(req))
    authz = signed.headers["authorization"]
    assert authz.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in authz
    assert "content-type" in authz and signed.headers["x-amz-security-token"] == "token"
