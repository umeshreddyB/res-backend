import os

# Support TEST_DATABASE_URL for running against PostgreSQL.
# Default stays SQLite for zero-config local development.
_test_db_url = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test.db")
os.environ["DATABASE_URL"] = _test_db_url
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = "test-secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsec"
os.environ["ENV"] = "test"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, engine
from app.main import app


def _is_sqlite() -> bool:
    return str(engine.url).startswith("sqlite")


@pytest.fixture()
def client():
    # Reset rate limiter state between tests
    from app.core.rate_limit import login_limiter, register_limiter
    login_limiter._hits.clear()
    register_limiter._hits.clear()

    if _is_sqlite():
        Base.metadata.drop_all(bind=engine)
    else:
        # PostgreSQL: drop all tables cleanly via CASCADE
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    with TestClient(app) as c:  # runs lifespan: creates tables + seeds admin
        yield c


def is_postgres() -> bool:
    """Helper for tests that need PostgreSQL features (e.g. row locks)."""
    return not _is_sqlite()
