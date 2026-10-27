"""Tests for Step 3: Admin Item and Category Management, Permissions, and Order Snapshot Preservation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io

from app.core.config import settings


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(client, email: str, password: str) -> str:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email: str = "customer_menu@example.com") -> str:
    r = client.post("/auth/register", json={"name": "Customer Menu", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def test_admin_category_management(client):
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "cust_cat@example.com")

    # 1. Non-admin forbidden
    r_cust = client.post("/admin/categories", json={"name": "Appetizers"}, headers=auth(cust))
    assert r_cust.status_code == 403

    # 2. Add category
    r_create = client.post("/admin/categories", json={"name": "Appetizers", "sort_order": 1}, headers=auth(admin))
    assert r_create.status_code == 201
    cat_id = r_create.json()["id"]
    assert r_create.json()["name"] == "Appetizers"

    # 3. List categories
    r_list = client.get("/admin/categories", headers=auth(admin))
    assert r_list.status_code == 200
    names = [c["name"] for c in r_list.json()]
    assert "Appetizers" in names

    # 4. Rename / update sort order
    r_patch = client.patch(f"/admin/categories/{cat_id}", json={"name": "Starters & Appetizers", "sort_order": 5}, headers=auth(admin))
    assert r_patch.status_code == 200
    assert r_patch.json()["name"] == "Starters & Appetizers"
    assert r_patch.json()["sort_order"] == 5

    # 5. Remove category
    r_del = client.delete(f"/admin/categories/{cat_id}", headers=auth(admin))
    assert r_del.status_code == 204

    # Verify deleted
    r_del_again = client.delete(f"/admin/categories/{cat_id}", headers=auth(admin))
    assert r_del_again.status_code == 404


def test_admin_item_crud_and_single_call_update(client):
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "cust_item@example.com")

    # Create category 1 & category 2
    cat1 = client.post("/admin/categories", json={"name": "Beverages"}, headers=auth(admin)).json()
    cat2 = client.post("/admin/categories", json={"name": "Hot Drinks"}, headers=auth(admin)).json()

    # 1. Non-admin forbidden
    r_noauth = client.post("/admin/items", json={
        "category_id": cat1["id"],
        "name": "Cold Coffee",
        "price": 120.0
    })
    assert r_noauth.status_code == 401

    r_cust = client.post("/admin/items", json={
        "category_id": cat1["id"],
        "name": "Cold Coffee",
        "price": 120.0
    }, headers=auth(cust))
    assert r_cust.status_code == 403

    # 2. Add an item
    r_add = client.post("/admin/items", json={
        "category_id": cat1["id"],
        "name": "Cold Coffee",
        "description": "Chilled brew",
        "price": 120.50,
        "is_veg": True,
        "is_available": True
    }, headers=auth(admin))
    assert r_add.status_code == 201
    item = r_add.json()
    item_id = item["id"]
    assert item["name"] == "Cold Coffee"
    assert item["price"] == 120.50
    assert item["is_veg"] is True
    assert item["is_available"] is True

    # 3. Change price, name, description, category, veg flag, and availability in ONE call
    r_update_all = client.patch(f"/admin/items/{item_id}", json={
        "category_id": cat2["id"],
        "name": "Special Hazelnut Coffee",
        "description": "Freshly ground with hazelnut syrup",
        "price": 175.00,
        "is_veg": False,
        "is_available": False
    }, headers=auth(admin))
    assert r_update_all.status_code == 200
    updated = r_update_all.json()
    assert updated["category_id"] == cat2["id"]
    assert updated["name"] == "Special Hazelnut Coffee"
    assert updated["description"] == "Freshly ground with hazelnut syrup"
    assert updated["price"] == 175.00
    assert updated["is_veg"] is False
    assert updated["is_available"] is False

    # 4. Mark available or sold out via dedicated availability endpoint
    r_avail = client.patch(f"/admin/items/{item_id}/availability", json={"is_available": True}, headers=auth(admin))
    assert r_avail.status_code == 200
    assert r_avail.json()["is_available"] is True

    # 5. Remove an item
    r_del = client.delete(f"/admin/items/{item_id}", headers=auth(admin))
    assert r_del.status_code == 204

    # Confirm item is gone
    r_get = client.get(f"/menu/items/{item_id}")
    assert r_get.status_code == 404


def test_changing_price_never_changes_existing_orders(client):
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "cust_order_snapshot@example.com")

    # Create category and item
    cat = client.post("/admin/categories", json={"name": "Main Course"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"],
        "name": "Paneer Butter Masala",
        "price": 300.00,
        "is_veg": True,
        "is_available": True
    }, headers=auth(admin)).json()
    item_id = item["id"]

    # Customer places order with 2 units @ 300.00 = 600.00
    pickup = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r_order = client.post("/orders", json={
        "order_type": "takeaway",
        "payment_option": "full_online",
        "pickup_time": pickup,
        "items": [{"menu_item_id": item_id, "quantity": 2}]
    }, headers=auth(cust))
    assert r_order.status_code == 201, r_order.text
    order = r_order.json()
    order_id = order["id"]
    assert order["total"] == 600.00
    assert order["items"][0]["unit_price"] == 300.00
    assert order["items"][0]["name"] == "Paneer Butter Masala"

    # Admin changes item price from 300 to 450, and renames item to "PBM Premium"
    r_price_change = client.patch(f"/admin/items/{item_id}", json={
        "price": 450.00,
        "name": "PBM Premium"
    }, headers=auth(admin))
    assert r_price_change.status_code == 200

    # Retrieve existing order: must retain its original unit_price (300.00), snapshot name, and total (600.00)
    r_order_fetch = client.get(f"/orders/{order_id}", headers=auth(cust))
    assert r_order_fetch.status_code == 200
    fetched_order = r_order_fetch.json()
    assert fetched_order["total"] == 600.00
    assert fetched_order["items"][0]["unit_price"] == 300.00
    assert fetched_order["items"][0]["name"] == "Paneer Butter Masala"


def test_deleting_item_or_category_preserves_past_orders(client):
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "cust_del_preserve@example.com")

    # Create category and item
    cat = client.post("/admin/categories", json={"name": "Dessert Delights"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"],
        "name": "Rasmalai",
        "price": 150.00,
        "is_veg": True,
        "is_available": True
    }, headers=auth(admin)).json()
    item_id = item["id"]

    # Place order
    pickup = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    r_order = client.post("/orders", json={
        "order_type": "takeaway",
        "payment_option": "full_online",
        "pickup_time": pickup,
        "items": [{"menu_item_id": item_id, "quantity": 3}]
    }, headers=auth(cust))
    assert r_order.status_code == 201
    order_id = r_order.json()["id"]

    # Delete the menu item
    r_del_item = client.delete(f"/admin/items/{item_id}", headers=auth(admin))
    assert r_del_item.status_code == 204

    # Order must still be completely readable and intact
    r_order_check = client.get(f"/orders/{order_id}", headers=auth(cust))
    assert r_order_check.status_code == 200
    data = r_order_check.json()
    assert data["total"] == 450.00
    assert data["items"][0]["name"] == "Rasmalai"
    assert data["items"][0]["unit_price"] == 150.00
    assert data["items"][0]["quantity"] == 3

    # Delete the category
    r_del_cat = client.delete(f"/admin/categories/{cat['id']}", headers=auth(admin))
    assert r_del_cat.status_code == 204

    # Order is still intact
    r_order_check2 = client.get(f"/orders/{order_id}", headers=auth(cust))
    assert r_order_check2.status_code == 200
    assert r_order_check2.json()["total"] == 450.00
