"""Tests requiring PostgreSQL (row locks, concurrent access).

Skipped automatically when the test suite runs on SQLite.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from tests.conftest import is_postgres

pytestmark = pytest.mark.skipif(not is_postgres(), reason="Requires PostgreSQL for row locks")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _register(client, email):
    r = client.post("/auth/register", json={"name": "Test", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return _login(client, email, "password123")


def test_concurrent_reservation_same_slot(client: TestClient):
    """Two simultaneous reservation requests for the same table and time slot.
    Exactly one must succeed (201), the other must fail (409).
    This validates the row-lock double-booking protection on PostgreSQL."""
    from datetime import datetime, timedelta, timezone

    admin = _login(client, "admin@example.com", "admin12345")
    # Create a single table with capacity 4
    client.post("/admin/tables", json={"number": 1, "capacity": 4}, headers=_auth(admin))

    # Register two different customers
    cust1 = _register(client, "racer1@example.com")
    cust2 = _register(client, "racer2@example.com")

    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=13, minute=0, second=0, microsecond=0).isoformat()
    payload = {"guests": 2, "start_time": start, "table_id": 1}

    results = [None, None]
    barrier = threading.Barrier(2, timeout=5)

    def book(idx, token):
        barrier.wait()  # make both threads fire at the same instant
        r = client.post("/reservations", json=payload, headers=_auth(token))
        results[idx] = r.status_code

    t1 = threading.Thread(target=book, args=(0, cust1))
    t2 = threading.Thread(target=book, args=(1, cust2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    status_codes = sorted(results)
    assert status_codes == [201, 409], f"Expected exactly one 201 and one 409, got {results}"


def test_concurrent_dine_in_same_slot(client: TestClient):
    """Two simultaneous dine-in order requests for the last free own_app table.
    Exactly one must succeed (201), the other must fail (409) and create nothing."""
    from datetime import datetime, timedelta, timezone

    admin = _login(client, "admin@example.com", "admin12345")
    # Category and item
    cat = client.post("/admin/categories", json={"name": "DineInCat"}, headers=_auth(admin)).json()
    item = client.post("/admin/items", json={"category_id": cat["id"], "name": "Dish", "price": "200.00"}, headers=_auth(admin)).json()
    # Create single table
    client.post("/admin/tables", json={"number": 10, "capacity": 4, "channel": "own_app"}, headers=_auth(admin))

    cust1 = _register(client, "racer_dine1@example.com")
    cust2 = _register(client, "racer_dine2@example.com")

    start = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": start,
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    results = [None, None]
    barrier = threading.Barrier(2, timeout=5)

    def book(idx, token):
        barrier.wait()
        r = client.post("/orders", json=payload, headers=_auth(token))
        results[idx] = r.status_code

    t1 = threading.Thread(target=book, args=(0, cust1))
    t2 = threading.Thread(target=book, args=(1, cust2))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    status_codes = sorted(results)
    assert status_codes == [201, 409], f"Expected exactly one 201 and one 409, got {results}"


def test_alembic_upgrade_head_on_empty_postgres():
    """Verify that alembic upgrade head runs cleanly on an empty PostgreSQL database."""
    import os
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set")

    engine = create_engine(test_db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
