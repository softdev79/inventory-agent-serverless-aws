"""Offline tests: DynamoDB via moto, LLM stubbed. Run: python -m pytest -q"""
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

os.environ.update({
    "AWS_REGION": "us-east-2", "AWS_DEFAULT_REGION": "us-east-2",
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "INVENTORY_TABLE": "t-inventory", "SESSIONS_TABLE": "t-sessions", "API_KEY": "test-key",
})


@pytest.fixture()
def aws():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-2")
        ddb.create_table(TableName="t-inventory", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "sku_id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "sku_id", "KeyType": "HASH"}])
        ddb.create_table(TableName="t-sessions", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"},
                                               {"AttributeName": "ts", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"},
                                    {"AttributeName": "ts", "KeyType": "RANGE"}])
        import db_client
        db_client._table.cache_clear()
        yield ddb


def test_seed_does_not_overwrite_edits(aws):
    import db_client
    db_client.seed_sample_data()
    db_client.upsert_inventory({"sku_id": "sku001", "name": "Widget A", "current_inventory": 5,
                                "avg_demand": 1.5, "lead_time": 2})
    db_client.seed_sample_data()
    row = db_client.get_inventory("SKU001")
    assert row["current_inventory"] == 5 and row["avg_demand"] == 1.5
    assert db_client.get_inventory("SKU002")["safety_stock"] == 15
    assert db_client.get_inventory("NOPE") is None


def test_tools(aws):
    import db_client
    from tools.inventory_lookup import lookup_inventory
    from tools.reorder_calculator import calculate_reorder
    db_client.seed_sample_data()
    assert "Current Inventory: 40 units" in lookup_inventory.invoke({"sku_id": "sku002"})
    out = calculate_reorder.invoke({"current_inventory": 40, "avg_demand": 8, "lead_time": 5, "safety_stock": 15})
    assert "Reorder point: 55.0" in out and "55 units" in out


def test_api(aws, monkeypatch):
    from fastapi.testclient import TestClient
    import agent
    import api
    monkeypatch.setattr(agent, "chat", lambda q, history=None: f"echo: {q}")
    with TestClient(api.app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        ui = client.get("/")
        assert ui.status_code == 200 and "Inventory Planning Agent" in ui.text
        assert client.post("/api/chat", json={"query": "hi"}).status_code == 401
        assert client.post("/api/chat", json={"query": "hi"}, headers={"X-API-Key": "bad"}).status_code == 403
        h = {"X-API-Key": "test-key"}
        r = client.post("/api/chat", json={"query": "reorder?", "sku_id": "SKU002"}, headers=h)
        assert r.status_code == 200 and r.json()["response"] == "echo: [SKU: SKU002] reorder?"
        sid = r.json()["session_id"]
        logged = aws.query(TableName="t-sessions", KeyConditionExpression="session_id = :s",
                           ExpressionAttributeValues={":s": {"S": sid}})["Items"]
        assert len(logged) == 1 and "expires_at" in logged[0]
        assert client.get("/api/inventory/sku002", headers=h).json()["current_inventory"] == 40
        assert client.post("/api/inventory", headers=h, json={"sku_id": "sku9", "current_inventory": 1,
                                                              "avg_demand": 1, "lead_time": 1}).status_code == 200
        assert client.get("/api/inventory/SKU9", headers=h).status_code == 200
        assert client.get("/api/inventory/missing", headers=h).status_code == 404
