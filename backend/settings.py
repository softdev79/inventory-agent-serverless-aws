"""Runtime configuration.

Locally: everything comes from .env (OPENAI_API_KEY etc.).
On Lambda: no long-lived LLM key exists anywhere. The function signs a short-term
Bedrock API key with its own IAM role (aws-bedrock-token-generator) and refreshes it
every 30 minutes. The internal API_KEY comes from SSM Parameter Store (SecureString).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai.gpt-oss-120b")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", f"https://bedrock-mantle.{REGION}.api.aws/v1")
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT_ID") or None

INVENTORY_TABLE = os.getenv("INVENTORY_TABLE", "inventory-agent-inventory")
SESSIONS_TABLE = os.getenv("SESSIONS_TABLE", "inventory-agent-sessions")
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

TOKEN_REFRESH_SECONDS = 30 * 60

_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0
_api_key: str | None = None


def llm_api_key() -> str:
    """Static key from env if set (local dev), else a fresh role-signed Bedrock token."""
    static = os.getenv("OPENAI_API_KEY")
    if static:
        return static
    global _token, _token_at
    with _lock:
        if _token is None or time.time() - _token_at > TOKEN_REFRESH_SECONDS:
            from aws_bedrock_token_generator import provide_token

            _token = provide_token(region=REGION, expiry=timedelta(hours=1))
            _token_at = time.time()
        return _token


def internal_api_key() -> str:
    """X-API-Key the UI must send. Env var wins; else read the SSM parameter once."""
    global _api_key
    if _api_key is None:
        if os.getenv("API_KEY"):
            _api_key = os.environ["API_KEY"]
        elif os.getenv("API_KEY_PARAM"):
            import boto3

            ssm = boto3.client("ssm", region_name=REGION)
            _api_key = ssm.get_parameter(Name=os.environ["API_KEY_PARAM"], WithDecryption=True)["Parameter"]["Value"]
        else:
            _api_key = "dev-secret"
    return _api_key
