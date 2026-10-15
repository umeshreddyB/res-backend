"""Security tests: access control, phone validation, rate limiting, change password."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="sectest@example.com"):
    r = client.post("/auth/register", json={"name": "Test", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def sign(order_id, payment_id, secret="test-secret"):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


# ---------- access control ----------

def test_customer_cannot_access_admin_routes(client):
    """Customer tokens must be rejected by all admin endpoints."""
    cust = register_customer(client)
    admin_endpoints = [
        ("POST", "/admin/categories", {"name": "X"}),
        ("GET", "/admin/orders", None),
        ("GET", "/admin/reservations", None),
        ("GET", "/admin/tables", None),
    ]
    for method, path, body in admin_endpoints:
        r = client.request(method, path, json=body, headers=auth(cust))
        assert r.status_code == 403, f"{method} {path} returned {r.status_code}, expected 403"


def test_user_cannot_read_other_users_orders(client):
    admin = login(client, "admin@example.com", "admin12345")
    # Set up menu
    cat = client.post("/admin/categories", json={"name": "Mains"}, headers=auth(admin)).json()
    client.post("/admin/items", json={"category_id": cat["id"], "name": "Dal", "price": "150.00"},
                headers=auth(admin))
    client.post("/admin/tables", json={"number": 1, "capacity": 4}, headers=auth(admin))

    user_a = register_customer(client, "a@example.com")
    user_b = register_customer(client, "b@example.com")

    # User A creates an order
    items = client.get("/menu").json()[0]["items"]
    order_a = client.post("/orders", json={"items": [{"menu_item_id": items[0]["id"], "quantity": 1}],
                                            "payment_option": "pay_at_pickup"}, headers=auth(user_a)).json()

    # User B cannot read User A's order
    assert client.get(f"/orders/{order_a['id']}", headers=auth(user_b)).status_code == 404

    # User B's /orders/me does not contain User A's order
    my_orders = client.get("/orders/me", headers=auth(user_b)).json()
    assert all(o["id"] != order_a["id"] for o in my_orders)


def test_user_cannot_read_other_users_reservations(client):
    admin = login(client, "admin@example.com", "admin12345")
    client.post("/admin/tables", json={"number": 1, "capacity": 4}, headers=auth(admin))

    user_a = register_customer(client, "a@example.com")
    user_b = register_customer(client, "b@example.com")

    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0).isoformat()
    res_a = client.post("/reservations", json={"guests": 2, "start_time": start},
                        headers=auth(user_a))
    assert res_a.status_code == 201

    # User B cannot cancel User A's reservation
    assert client.post(f"/reservations/{res_a.json()['id']}/cancel",
                       headers=auth(user_b)).status_code == 404

    # User B's /reservations/me does not contain User A's reservation
    my_res = client.get("/reservations/me", headers=auth(user_b)).json()
    assert all(r["id"] != res_a.json()["id"] for r in my_res)


# ---------- phone validation ----------

def test_phone_validation(client):
    # Valid Indian phones
    valid_phones = ["+919876543210", "9876543210", "+91 9876543210"]
    for i, phone in enumerate(valid_phones):
        r = client.post("/auth/register", json={"name": "X", "email": f"validph{i}@test.com",
                                                  "password": "password123", "phone": phone})
        assert r.status_code == 201, f"Valid phone {phone} rejected: {r.text}"

    # Reset limiter so invalid phone checks don't hit the rate limit
    from app.core.rate_limit import register_limiter
    register_limiter._hits.clear()

    # Invalid phones
    invalid_phones = ["1234567890", "+12125551234", "123"]
    for i, phone in enumerate(invalid_phones):
        r = client.post("/auth/register", json={"name": "X", "email": f"badph{i}@test.com",
                                                  "password": "password123", "phone": phone})
        assert r.status_code == 400, f"Invalid phone {phone} accepted: {r.text}"


# ---------- change password ----------

def test_change_password(client):
    cust = register_customer(client, "changepw@example.com")

    # Wrong current password
    r = client.post("/auth/change-password",
                    json={"current_password": "wrongpw123", "new_password": "newpass12345"},
                    headers=auth(cust))
    assert r.status_code == 400

    # Correct current password
    r = client.post("/auth/change-password",
                    json={"current_password": "password123", "new_password": "newpass12345"},
                    headers=auth(cust))
    assert r.status_code == 200

    # Can login with new password
    new_token = login(client, "changepw@example.com", "newpass12345")
    assert new_token

    # Old password no longer works
    r = client.post("/auth/login", json={"email": "changepw@example.com", "password": "password123"})
    assert r.status_code == 401


# ---------- admin create staff ----------

def test_admin_create_staff(client):
    admin = login(client, "admin@example.com", "admin12345")

    # Admin creates a staff user
    r = client.post("/auth/admin/create-user",
                    json={"name": "Staff", "email": "staff@example.com",
                          "password": "staffpass123", "role": "admin"},
                    headers=auth(admin))
    assert r.status_code == 201
    assert r.json()["role"] == "admin"

    # Customer cannot create staff
    cust = register_customer(client, "nocreate@example.com")
    r = client.post("/auth/admin/create-user",
                    json={"name": "Hacker", "email": "hacker@example.com",
                          "password": "hackpass123", "role": "admin"},
                    headers=auth(cust))
    assert r.status_code == 403


# ---------- rate limiting ----------

def test_rate_limit_login(client):
    """Login endpoint should be rate limited."""
    from app.core.rate_limit import login_limiter
    # Reset the limiter state for this test
    login_limiter._hits.clear()

    # Make requests up to the limit (default 10)
    for i in range(10):
        client.post("/auth/login", json={"email": "nobody@test.com", "password": "wrong"})

    # Next request should be rate limited
    r = client.post("/auth/login", json={"email": "nobody@test.com", "password": "wrong"})
    assert r.status_code == 429


def test_rate_limit_register(client):
    """Register endpoint should be rate limited."""
    from app.core.rate_limit import register_limiter
    register_limiter._hits.clear()

    for i in range(5):
        client.post("/auth/register", json={"name": "X", "email": f"rl{i}@test.com",
                                             "password": "password123"})

    r = client.post("/auth/register", json={"name": "X", "email": "rl99@test.com",
                                             "password": "password123"})
    assert r.status_code == 429
