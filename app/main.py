import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .core.config import settings
from .core.security import hash_password
from .db import Base, SessionLocal, engine
from .models import Role, User
from .routers import (auth, menu, orders, payments, reservations, settings as
                     settings_router, tables)
from .services.abandoned_orders import abandoned_order_loop

logging.basicConfig(level=logging.INFO)


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

for r in (auth.router, menu.router, menu.admin_router, tables.router, tables.admin_router,
          reservations.router, reservations.admin_router, orders.router, orders.admin_router,
          payments.router, settings_router.router, settings_router.admin_router):
    app.include_router(r)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
