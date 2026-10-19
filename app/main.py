import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from .core.config import settings
from .core.logging import request_id_ctx, setup_logging
from .core.security import hash_password
from .db import Base, SessionLocal, engine
from .models import Role, User
from .routers import (auth, menu, orders, payments, petpooja, reports, reservations,
                      settings as settings_router, tables)
from .services.abandoned_orders import abandoned_order_loop

setup_logging(settings.log_level)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.env == "prod":
        if settings.secret_key == "change-me":
            raise RuntimeError("Set a strong SECRET_KEY before running in production")
        if settings.admin_password == "admin12345":
            raise RuntimeError("Change the default ADMIN_PASSWORD before running in production")
        if not settings.cors_list:
            raise RuntimeError("Set CORS_ORIGINS before running in production")
    # Dev/test convenience. In production use Alembic: alembic upgrade head
    if settings.env != "prod":
        Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:  # first admin account comes from .env
        if not db.scalar(select(User).where(User.role == Role.admin)):
            db.add(User(name="Admin", email=settings.admin_email.lower(),
                        password_hash=hash_password(settings.admin_password), role=Role.admin))
            db.commit()
    # Start background cleanup for abandoned unpaid orders
    cleanup_task = asyncio.create_task(abandoned_order_loop())
    yield
    cleanup_task.cancel()


app = FastAPI(title="Restaurant API", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# --- Request ID Middleware ---
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    raw_id = request.headers.get("X-Request-ID")
    req_id = raw_id.strip() if raw_id and raw_id.strip() else uuid.uuid4().hex
    token = request_id_ctx.set(req_id)
    request.state.request_id = req_id
    try:
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response
    finally:
        request_id_ctx.reset(token)


# --- Consistent JSON Error Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, "request_id", None) or request_id_ctx.get("-")
    headers = dict(exc.headers or {})
    headers["X-Request-ID"] = req_id
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error": {
                "code": exc.status_code,
                "message": str(exc.detail),
                "request_id": req_id,
            },
        },
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", None) or request_id_ctx.get("-")
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "error": {
                "code": 422,
                "message": "Validation error",
                "details": exc.errors(),
                "request_id": req_id,
            },
        },
        headers={"X-Request-ID": req_id},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", None) or request_id_ctx.get("-")
    log.error("Unhandled exception on %s %s [req_id=%s]: %s", request.method, request.url.path, req_id, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error": {
                "code": 500,
                "message": "Internal server error",
                "request_id": req_id,
            },
        },
        headers={"X-Request-ID": req_id},
    )


for r in (auth.router, menu.router, menu.admin_router, tables.router, tables.admin_router,
          reservations.router, reservations.admin_router, orders.router, orders.admin_router,
          payments.router, settings_router.router, settings_router.admin_router,
          petpooja.router, petpooja.admin_router, reports.admin_router):
    app.include_router(r)

os.makedirs(settings.media_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")


@app.get("/health", tags=["system"])
def health(request: Request):
    """Health check endpoint. Checks database connectivity and returns 503 if unavailable."""
    req_id = getattr(request.state, "request_id", None) or request_id_ctx.get("-")
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "request_id": req_id}
    except Exception as e:
        log.error("Health check database failure: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "database": "unavailable",
                "detail": "Database connection failed",
                "error": {
                    "code": 503,
                    "message": "Database connection failed",
                    "request_id": req_id,
                },
            },
            headers={"X-Request-ID": req_id},
        )
