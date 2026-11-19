"""Prove the Bedrock OpenAI-compatible credentials work, including tool calling.

Reads OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_PROJECT_ID (and optional OPENAI_MODEL)
from the environment or from .env. Prints no secrets.

    python scripts/smoke_model.py
"""
import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

missing = [k for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL") if not os.getenv(k)]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")

model = os.getenv("OPENAI_MODEL", "openai.gpt-oss-120b")
client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url=os.environ["OPENAI_BASE_URL"],
    project=os.getenv("OPENAI_PROJECT_ID") or None,
)

tools = [{
    "type": "function",
    "function": {
        "name": "lookup_inventory",
        "description": "Look up inventory data for a SKU.",
        "parameters": {
            "type": "object",
            "properties": {"sku_id": {"type": "string"}},
            "required": ["sku_id"],
        },
    },
}]

resp = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "How much stock do we have of SKU002?"}],
    tools=tools,
)
msg = resp.choices[0].message
calls = msg.tool_calls or []
print(f"base_url : {os.environ['OPENAI_BASE_URL']}")
print(f"model    : {model}")
print(f"project  : {'set' if os.getenv('OPENAI_PROJECT_ID') else 'not set'}")
print(f"finish   : {resp.choices[0].finish_reason}")
for c in calls:
    print(f"tool call: {c.function.name}({c.function.arguments})")
if not calls:
    print(f"text     : {msg.content!r}")
    sys.exit("FAIL: model did not call the tool")
assert json.loads(calls[0].function.arguments).get("sku_id", "").upper() == "SKU002"
print("OK: credentials and tool calling work")
