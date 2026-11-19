"""DynamoDB data access. Same functions as the SQLite version, so tools and API are unchanged.

Tables (on-demand billing, always-free tier covers a demo):
  inventory: PK sku_id
  sessions : PK session_id, SK ts, TTL attribute expires_at
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import settings

FIELDS = ["sku_id", "name", "current_inventory", "avg_demand", "lead_time", "safety_stock"]

SAMPLES = [
    {"sku_id": "SKU001", "name": "Widget A", "current_inventory": 100, "avg_demand": 10.0, "lead_time": 7, "safety_stock": 20},
    {"sku_id": "SKU002", "name": "Widget B", "current_inventory": 40, "avg_demand": 8.0, "lead_time": 5, "safety_stock": 15},
    {"sku_id": "SKU003", "name": "Gadget X", "current_inventory": 200, "avg_demand": 5.0, "lead_time": 10, "safety_stock": 10},
]


@lru_cache(maxsize=None)
def _table(name: str):
    return boto3.resource("dynamodb", region_name=settings.REGION).Table(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _to_item(data: dict) -> dict:
    item = {
        "sku_id": str(data["sku_id"]).upper(),
        "name": data.get("name") or "",
        "current_inventory": int(data["current_inventory"]),
        "avg_demand": Decimal(str(data["avg_demand"])),
        "lead_time": int(data["lead_time"]),
        "safety_stock": int(data.get("safety_stock") or 0),
        "updated_at": _now(),
    }
    return item


def _from_item(item: dict) -> dict:
    out = dict(item)
    for k in ("current_inventory", "lead_time", "safety_stock"):
        if k in out:
            out[k] = int(out[k])
    if "avg_demand" in out:
        out["avg_demand"] = float(out["avg_demand"])
    return out


def init_db() -> None:
    """Tables are created by the SAM template; nothing to do at runtime."""


def get_inventory(sku_id: str) -> Optional[dict]:
    resp = _table(settings.INVENTORY_TABLE).get_item(Key={"sku_id": sku_id.upper()})
    item = resp.get("Item")
    return _from_item(item) if item else None


def upsert_inventory(data: dict) -> None:
    _table(settings.INVENTORY_TABLE).put_item(Item=_to_item(data))


def log_session(session_id: str, user_query: str, agent_response: str, sku_ref: Optional[str] = None) -> None:
    item = {
        "session_id": session_id,
        "ts": _now(),
        "user_query": user_query,
        "agent_response": agent_response,
        "expires_at": int(time.time()) + settings.SESSION_TTL_DAYS * 86400,
    }
    if sku_ref:
        item["sku_ref"] = sku_ref
    _table(settings.SESSIONS_TABLE).put_item(Item=item)


def seed_sample_data() -> None:
    """Insert sample SKUs only if missing, so edits survive cold starts."""
    table = _table(settings.INVENTORY_TABLE)
    for sample in SAMPLES:
        try:
            table.put_item(Item=_to_item(sample), ConditionExpression="attribute_not_exists(sku_id)")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
