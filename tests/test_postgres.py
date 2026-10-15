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
