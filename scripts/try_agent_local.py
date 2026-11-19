"""Run the real agent against Bedrock with an in-memory DynamoDB (moto). No AWS resources used.

    python scripts/try_agent_local.py "Should I reorder SKU002?"
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("INVENTORY_TABLE", "local-inventory")
os.environ.setdefault("SESSIONS_TABLE", "local-sessions")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

question = " ".join(sys.argv[1:]) or "Should I reorder SKU002?"

import settings  # noqa: E402  (loads .env)

# Sign the Bedrock token with your real AWS login BEFORE moto swaps in fake credentials.
os.environ.setdefault("OPENAI_API_KEY", settings.llm_api_key())

with mock_aws():

    boto3.client("dynamodb", region_name=settings.REGION).create_table(
        TableName=settings.INVENTORY_TABLE, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "sku_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "sku_id", "KeyType": "HASH"}])
    import agent  # noqa: E402
    import db_client  # noqa: E402

    db_client.seed_sample_data()
    t = time.time()
    answer, calls = agent.chat_with_trace(question)
    print(f"Q: {question}\n")
    for c in calls:
        print(f"  tool -> {c}")
    print(f"\nA ({time.time() - t:.1f}s):\n{answer}")
