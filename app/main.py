from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .admin import router as admin_router
from .ai import router as ai_router
from .analytics import router as analytics_router
from .auth import router as auth_router
from .billing import router as billing_router
from .config import get_settings
from .connections import router as connections_router
from .database import Base, SessionLocal, engine
from .events import router as events_router
from .media_routes import router as media_router
from .posts import router as posts_router
from .providers import ProviderError, UploadPostClient
from .webhooks import router as webhooks_router

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.is_production:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="Reverb API",
    version="1.0.0",
    description="Authentication, creator connections, resilient media uploads and cross-platform publishing for Reverb.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Content-Range", "Idempotency-Key", "X-Request-ID", "X-Chunk-SHA256"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/v1/") else "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", None)
    if isinstance(exc.detail, dict):
        body = {**exc.detail, "request_id": request_id}
    else:
        body = {"code": "request_failed", "message": str(exc.detail), "request_id": request_id}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    field_errors = {".".join(map(str, item["loc"][1:])): item["msg"] for item in exc.errors()}
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Check the highlighted fields",
            "field_errors": field_errors,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled request error %s", request_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "An unexpected error occurred",
            "request_id": request_id,
        },
    )


app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(analytics_router)
app.include_router(ai_router)
app.include_router(admin_router)
app.include_router(events_router)
app.include_router(connections_router)
app.include_router(media_router)
app.include_router(posts_router)
app.include_router(webhooks_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    async with SessionLocal() as db:
        await db.execute(text("SELECT 1"))
    provider = "not_configured"
    if settings.upload_post_api_key:
        try:
            await UploadPostClient().verify_account()
            provider = "ok"
        except ProviderError:
            provider = "unavailable"
    return {"status": "ok", "database": "ok", "upload_post": provider}
