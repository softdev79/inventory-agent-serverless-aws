"""FastAPI app. All routes live under /api so CloudFront can send /api/* to Lambda
and everything else to the static UI in S3. On Lambda, Mangum adapts Function URL events."""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security.api_key import APIKeyHeader
from mangum import Mangum
from pydantic import BaseModel, Field

import agent as agent_module
import db_client
import settings

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("inventory-agent")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


def verify_api_key(key: str = Security(api_key_header)) -> str:
    if not secrets.compare_digest(key, settings.internal_api_key()):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        db_client.seed_sample_data()
    except Exception:  # never block start-up on seeding
        log.exception("seeding failed")
    yield


app = FastAPI(title="Inventory Planning Agent API (serverless)", lifespan=lifespan)
router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    session_id: Optional[str] = None
    sku_id: Optional[str] = None
    history: Optional[list[dict]] = None


class ChatResponse(BaseModel):
    session_id: str
    response: str


class InventoryItem(BaseModel):
    sku_id: str = Field(min_length=1, max_length=40)
    name: Optional[str] = None
    current_inventory: int = Field(ge=0)
    avg_demand: float = Field(ge=0)
    lead_time: int = Field(ge=0)
    safety_stock: int = Field(default=0, ge=0)


@router.get("/health")
def health():
    return {"status": "ok", "model": settings.OPENAI_MODEL, "region": settings.REGION}


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    query = f"[SKU: {req.sku_id}] {req.query}" if req.sku_id else req.query

    try:
        response = agent_module.chat(query, history=req.history or [])
    except Exception as e:  # surface LLM/tool errors instead of a bare 500
        log.exception("agent error")
        raise HTTPException(status_code=502, detail=f"Agent error ({type(e).__name__}): {e}")

    db_client.log_session(session_id=session_id, user_query=req.query, agent_response=response, sku_ref=req.sku_id)
    return ChatResponse(session_id=session_id, response=response)


@router.get("/inventory/{sku_id}", dependencies=[Depends(verify_api_key)])
def get_inventory(sku_id: str):
    row = db_client.get_inventory(sku_id.upper())
    if not row:
        raise HTTPException(status_code=404, detail=f"SKU '{sku_id}' not found")
    return row


@router.post("/inventory", dependencies=[Depends(verify_api_key)])
def upsert_inventory(item: InventoryItem):
    db_client.upsert_inventory(item.model_dump())
    return {"status": "ok", "sku_id": item.sku_id.upper()}


app.include_router(router)

# function-url mode: the Lambda also serves the static page (cloudfront mode serves it from S3).
_UI_FILE = Path(__file__).parent / "static" / "index.html"
if not _UI_FILE.exists():  # local dev: repo layout
    _UI_FILE = Path(__file__).parent.parent / "frontend" / "index.html"

if os.getenv("SERVE_UI", "true").lower() == "true":

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(_UI_FILE, media_type="text/html", headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                       "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        })

# Lambda entry point (Function URL events = API Gateway v2 payload format)
handler = Mangum(app, lifespan="auto")
