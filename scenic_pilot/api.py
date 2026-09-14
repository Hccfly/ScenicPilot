from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scenic_pilot import __version__
from scenic_pilot.config import PROJECT_ROOT, Settings
from scenic_pilot.persistence import IdempotencyConflict
from scenic_pilot.runtime import get_runtime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    scenic_id: str = "zhuhai-chimelong"


class ChatResponse(BaseModel):
    answer: str
    answer_status: str = "answered"
    intent: str
    citations: list[dict]
    claims: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    answer_trace: dict = Field(default_factory=dict)
    plan: dict | None
    tool_results: list[dict] = Field(default_factory=list)
    planning_trace: dict = Field(default_factory=dict)
    violations: list[dict]
    constraint_status: str = "not_applicable"
    revision_count: int = 0
    revision_history: list[dict] = Field(default_factory=list)
    approval_status: str = "not_required"
    tool_call_count: int = 0
    tool_call_limit: int = 0
    governance_trace: dict = Field(default_factory=dict)
    trace: list[str]
    retrieval_trace: dict


class SessionStartRequest(ChatRequest):
    thread_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    require_approval: bool = False


class SessionResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    approved: bool
    reason: str = Field(default="", max_length=500)


class SessionEnvelope(BaseModel):
    status: str
    thread_id: str
    idempotency_key: str | None = None
    idempotency_replayed: bool = False
    interrupts: list[dict] = Field(default_factory=list)
    result: dict | None = None
    error: dict | None = None
    next_nodes: list[str] = Field(default_factory=list)
    checkpoint: dict | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    if get_runtime.cache_info().currsize:
        get_runtime().close()
        get_runtime.cache_clear()


app = FastAPI(
    title="ScenicPilot",
    version=__version__,
    description="Grounded scenic Q&A and governed itinerary planning API.",
    lifespan=lifespan,
)
app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "web"), name="assets")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "web" / "index.html")


def _readiness() -> dict[str, Any]:
    settings = Settings.from_env()
    checks = {
        "knowledge_data": settings.data_path.is_file(),
        "web_ui": (PROJECT_ROOT / "web" / "index.html").is_file(),
        "retrieval_index": (
            True
            if settings.retrieval_index_path is None
            else (settings.retrieval_index_path / "manifest.json").is_file()
        ),
    }
    ready = all(checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "version": __version__,
        "scenic_id": settings.scenic_id,
        "mode": (
            "offline-demo"
            if settings.tool_mode == "offline" and settings.llm_provider == "offline"
            else "configured"
        ),
        "tool_mode": settings.tool_mode,
        "llm_provider": settings.llm_provider,
        "checks": checks,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    readiness = _readiness()
    return {
        **readiness,
        "status": "ok" if readiness["status"] == "ready" else "degraded",
    }


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    readiness = _readiness()
    return JSONResponse(
        readiness,
        status_code=200 if readiness["status"] == "ready" else 503,
    )


@app.post("/v1/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    result = get_runtime().invoke(request.message, scenic_id=request.scenic_id)
    return ChatResponse(**result)


@app.post("/v1/sessions", response_model=SessionEnvelope)
def start_session(request: SessionStartRequest) -> SessionEnvelope:
    try:
        result = get_runtime().start(
            request.message,
            scenic_id=request.scenic_id,
            thread_id=request.thread_id,
            idempotency_key=request.idempotency_key,
            require_approval=request.require_approval,
        )
    except IdempotencyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return SessionEnvelope(**result)


@app.post("/v1/sessions/resume", response_model=SessionEnvelope)
def resume_session(request: SessionResumeRequest) -> SessionEnvelope:
    try:
        result = get_runtime().resume(
            thread_id=request.thread_id,
            approved=request.approved,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
        )
    except IdempotencyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return SessionEnvelope(**result)


@app.get("/v1/sessions/{thread_id}", response_model=SessionEnvelope)
def get_session(thread_id: str) -> SessionEnvelope:
    try:
        result = get_runtime().get_session(thread_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return SessionEnvelope(**result)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _streaming_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        yield _sse("started", {"scenic_id": request.scenic_id})
        try:
            result = await asyncio.to_thread(
                get_runtime().invoke,
                request.message,
                scenic_id=request.scenic_id,
            )
        except Exception as error:
            yield _sse(
                "failed",
                {
                    "code": "chat_execution_failed",
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                },
            )
            return
        yield _sse("completed", result)

    return _streaming_response(events())


@app.post("/v1/sessions/stream")
async def start_session_stream(request: SessionStartRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        yield _sse(
            "started",
            {
                "scenic_id": request.scenic_id,
                "thread_id": request.thread_id,
            },
        )
        try:
            envelope = await asyncio.to_thread(
                get_runtime().start,
                request.message,
                scenic_id=request.scenic_id,
                thread_id=request.thread_id,
                idempotency_key=request.idempotency_key,
                require_approval=request.require_approval,
            )
        except IdempotencyConflict as error:
            yield _sse(
                "failed",
                {"code": "idempotency_conflict", "message": str(error)},
            )
            return
        except (ValueError, RuntimeError) as error:
            yield _sse(
                "failed",
                {
                    "code": "session_start_failed",
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                },
            )
            return
        event = (
            "approval_required"
            if envelope.get("status") == "awaiting_approval"
            else "completed"
            if envelope.get("status") == "completed"
            else "failed"
        )
        yield _sse(event, envelope)

    return _streaming_response(events())
