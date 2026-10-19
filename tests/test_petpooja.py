"""Tests for Task 8: PetPooja POS Integration Structure and Mocked Calls."""
import httpx
from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.models import Order, OrderStatus
from app.services.petpooja import (map_order_to_petpooja_payload, push_order,
                                   retry_failed_pushes)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="cust_pp@example.com"):
    r = client.post("/auth/register", json={"name": "Cust", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_order(client, cust_token):
    admin = login(client, "admin@example.com", "admin12345")
    cat = client.post("/admin/categories", json={"name": "Biryani"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"],
        "name": "Chicken Biryani",
        "price": "350.00"
    }, headers=auth(admin)).json()
    prev = settings.petpooja_enabled
    settings.petpooja_enabled = False
    from datetime import datetime, timedelta, timezone
    pickup = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
    try:
        order = client.post("/orders", json={
            "items": [{"menu_item_id": item["id"], "quantity": 2}],
            "payment_option": "pay_at_pickup",
            "customer_name": "Cust",
            "pickup_time": pickup,
        }, headers=auth(admin)).json()
    finally:
        settings.petpooja_enabled = prev
    return order


def test_push_skipped_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "petpooja_enabled", False)
    cust = register_customer(client, "disabled@test.com")
    order = setup_order(client, cust)

    result = push_order(order["id"])
    assert result is False

    with SessionLocal() as db:
        o = db.get(Order, order["id"])
        assert o.petpooja_status is None
        assert o.petpooja_attempts == 0


def test_successful_petpooja_push_mocked(client, monkeypatch):
    monkeypatch.setattr(settings, "petpooja_enabled", True)
    monkeypatch.setattr(settings, "petpooja_app_key", "test-app-key")
    monkeypatch.setattr(settings, "petpooja_app_secret", "test-app-secret")
    cust = register_customer(client, "success@test.com")
    order = setup_order(client, cust)

    def mock_handler(request: httpx.Request):
        assert request.url.path == "/orders/save"
        assert request.headers.get("X-App-Key") == "test-app-key"
        return httpx.Response(200, json={"status": "success", "petpooja_order_id": "pp_order_999"})

    mock_client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    success = push_order(order["id"], http_client=mock_client)
    assert success is True

    with SessionLocal() as db:
        o = db.get(Order, order["id"])
        assert o.petpooja_status == "success"
        assert o.petpooja_order_id == "pp_order_999"
        assert o.petpooja_attempts == 1
        assert o.petpooja_pushed_at is not None
        assert o.petpooja_last_error is None


def test_failed_petpooja_push_and_retry(client, monkeypatch):
    monkeypatch.setattr(settings, "petpooja_enabled", True)
    cust = register_customer(client, "failed@test.com")
    order = setup_order(client, cust)

    # First attempt: failure (HTTP 500)
    def fail_handler(request: httpx.Request):
        return httpx.Response(500, json={"error": "PetPooja server down"})

    mock_fail_client = httpx.Client(transport=httpx.MockTransport(fail_handler))
    success = push_order(order["id"], http_client=mock_fail_client)
    assert success is False

    with SessionLocal() as db:
        o = db.get(Order, order["id"])
        assert o.petpooja_status == "failed"
        assert o.petpooja_attempts == 1
        assert "500" in o.petpooja_last_error

    # Second attempt: retry with success
    def ok_handler(request: httpx.Request):
        return httpx.Response(200, json={"order_id": "pp_retry_ok"})

    mock_ok_client = httpx.Client(transport=httpx.MockTransport(ok_handler))
    retried_count = retry_failed_pushes(max_attempts=3, http_client=mock_ok_client)
    assert retried_count == 1

    with SessionLocal() as db:
        o = db.get(Order, order["id"])
        assert o.petpooja_status == "success"
        assert o.petpooja_order_id == "pp_retry_ok"
        assert o.petpooja_attempts == 2
        assert o.petpooja_last_error is None


def test_map_order_to_petpooja_payload(client):
    cust = register_customer(client, "mapper@test.com")
    order_data = setup_order(client, cust)

    with SessionLocal() as db:
        order = db.scalar(select(Order).where(Order.id == order_data["id"]))
        payload = map_order_to_petpooja_payload(order)
        assert payload["order_id"] == str(order.id)
        assert payload["order_type"] == "takeaway"
        assert payload["customer"]["name"] == "Cust"
        assert len(payload["order_items"]) == 1
        assert payload["order_items"][0]["name"] == "Chicken Biryani"
        assert payload["order_items"][0]["quantity"] == 2
        assert payload["payment"]["total"] == 700.0


def test_petpooja_callbacks(client):
    # Order status callback
    r_cb = client.post("/petpooja/callback/order-status", json={
        "order_id": "pp_123",
        "status": "food_ready"
    })
    assert r_cb.status_code == 200

    # Menu sync callback
    r_menu = client.post("/petpooja/callback/menu-sync", json={
        "items": [{"name": "Test Item"}]
    })
    assert r_menu.status_code == 200


def test_admin_petpooja_endpoints(client, monkeypatch):
    monkeypatch.setattr(settings, "petpooja_enabled", True)
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "unauth@test.com")

    # Customer cannot call retry
    r_cust = client.post("/admin/petpooja/retry", headers=auth(cust))
    assert r_cust.status_code == 403

    # Admin can trigger retry
    r_admin = client.post("/admin/petpooja/retry", headers=auth(admin))
    assert r_admin.status_code == 200
    assert "retried_success_count" in r_admin.json()
