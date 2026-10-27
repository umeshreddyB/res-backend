"""Tests for Step 1: Make orders impossible to fake.
Ensures strict schema validation, extra field rejection (422), DB-driven pricing, and availability enforcement.
"""
from datetime import datetime, timedelta, timezone

from app.core.timeutils import LOCAL_TZ


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="cust_strict@example.com"):
    r = client.post("/auth/register", json={"name": "Strict Customer", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_menu(client, admin):
    cat = client.post("/admin/categories", json={"name": "Curries"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"],
        "name": "Butter Chicken",
        "price": "350.00",
        "is_veg": False,
        "is_available": True
    }, headers=auth(admin)).json()
    return item


def pickup_time_str():
    # 2 days in future at 14:00 local time
    future_date = (datetime.now(LOCAL_TZ) + timedelta(days=2)).date()
    dt = datetime(future_date.year, future_date.month, future_date.day, 14, 0, tzinfo=LOCAL_TZ)
    return dt.isoformat()


def test_order_rejects_extra_fields_on_item_line(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu(client, admin)
    cust = register_customer(client, "extra_item_fields@example.com")

    # Extra 'price' on item
    r_price = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": item["id"], "quantity": 1, "price": 10.0}],
    }, headers=auth(cust))
    assert r_price.status_code == 422
    assert "price" in r_price.text

    # Extra 'name' on item
    r_name = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": item["id"], "quantity": 1, "name": "Fake Name"}],
    }, headers=auth(cust))
    assert r_name.status_code == 422
    assert "name" in r_name.text

    # Extra 'is_available' on item
    r_avail = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": item["id"], "quantity": 1, "is_available": True}],
    }, headers=auth(cust))
    assert r_avail.status_code == 422
    assert "is_available" in r_avail.text


def test_order_rejects_extra_fields_at_order_level(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu(client, admin)
    cust = register_customer(client, "extra_order_fields@example.com")

    # Extra 'total' at order level
    r_total = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "total": 50.0,
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r_total.status_code == 422
    assert "total" in r_total.text

    # Extra 'price' at order level
    r_price = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "price": 50.0,
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r_price.status_code == 422
    assert "price" in r_price.text


def test_order_rejects_unknown_item_id(client):
    admin = login(client, "admin@example.com", "admin12345")
    setup_menu(client, admin)
    cust = register_customer(client, "unknown_item@example.com")

    r = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": 99999, "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400
    assert "unknown menu item id" in r.text.lower()


def test_order_rejects_unavailable_item(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu(client, admin)
    client.patch(f"/admin/items/{item['id']}/availability", json={"is_available": False}, headers=auth(admin))

    cust = register_customer(client, "unavailable_item@example.com")
    r = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 409
    assert item["name"] in r.text


def test_order_total_calculated_from_db_prices_and_snapshots_saved(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu(client, admin)  # price 350.00
    cust = register_customer(client, "db_calc_cust@example.com")

    r = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": pickup_time_str(),
        "items": [{"menu_item_id": item["id"], "quantity": 3}],
    }, headers=auth(cust))
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["total"] == 1050.0
    assert len(order["items"]) == 1
    assert order["items"][0]["name"] == "Butter Chicken"
    assert order["items"][0]["unit_price"] == 350.0
    assert order["items"][0]["quantity"] == 3
