"""ClawVia FastAPI server."""

import time
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

import asyncio

import agent
import config
from memory import db
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("server")

app = FastAPI(
    title="ClawVia",
    description="Lightweight AI agent system for Android",
    version="1.0.0",
)

# ── CORS — restrict to localhost by default ───────────────────────────────
_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:8001",
    "http://127.0.0.1",
    "http://127.0.0.1:8001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ── API Key Authentication Middleware ─────────────────────────────────────

@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """Require X-API-Key header on all endpoints except /health."""
    # Skip auth for health check
    if request.url.path == "/health":
        return await call_next(request)

    # If no API key is configured, allow (development mode)
    if not config.API_SECRET_KEY:
        return await call_next(request)

    api_key = request.headers.get("X-API-Key", "")
    if api_key != config.API_SECRET_KEY:
        log.warning("Unauthorized API request from %s to %s",
                   request.client.host if request.client else "unknown",
                   request.url.path)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )

    return await call_next(request)


# ── Rate Limiting Middleware ──────────────────────────────────────────────

_RATE_LIMIT = 30  # requests per minute
_rate_tracker = defaultdict(list)  # ip -> [timestamps]


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """Simple in-memory rate limiting: 30 requests/minute per IP."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60  # seconds

    # Clean old entries
    _rate_tracker[client_ip] = [
        ts for ts in _rate_tracker[client_ip] if now - ts < window
    ]

    if len(_rate_tracker[client_ip]) >= _RATE_LIMIT:
        log.warning("Rate limit exceeded for %s", client_ip)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Max 30 requests per minute."},
        )

    _rate_tracker[client_ip].append(now)
    return await call_next(request)


# ── Request / Response Models ─────────────────────────────────────────────

class TaskRequest(BaseModel):
    task: str
    session_id: Optional[int] = Field(None, description="Session ID (uses active if omitted)")


class SessionCreateRequest(BaseModel):
    title: str = "New Session"


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tools": len(registry),
        "sessions": len(db.list_sessions()),
    }


@app.get("/tools")
async def list_tools():
    return {"tools": registry.get_all_metadata()}


@app.post("/task")
async def run_task(req: TaskRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty")
    log.info("API task: %s", req.task[:100])
    try:
        result = await asyncio.to_thread(agent.run, req.task, req.session_id)
        return {"result": result}
    except Exception as exc:
        log.error("Task failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/sessions")
async def list_sessions():
    return {"sessions": db.list_sessions()}


@app.post("/sessions")
async def create_session(req: SessionCreateRequest):
    session = db.create_session(req.title)
    return {"session": session}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    sessions = db.list_sessions()
    if not any(s["id"] == session_id for s in sessions):
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


@app.get("/sessions/{session_id}/history")
async def session_history(session_id: int):
    history = db.get_session_history(session_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return history