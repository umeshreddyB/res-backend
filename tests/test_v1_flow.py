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


def sign(order_id, payment_id, secret="test-secret"):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def setup_menu_and_table(client, admin):
    cat = client.post("/admin/categories", json={"name": "Mains"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={"category_id": cat["id"], "name": "Paneer Butter Masala",
                                              "price": "250.00"}, headers=auth(admin)).json()
    client.post("/admin/tables", json={"number": 1, "capacity": 4}, headers=auth(admin))
    return item


def register_customer(client, email="cust@example.com"):
    r = client.post("/auth/register", json={"name": "Asha", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def test_admin_required(client):
    cust = register_customer(client)
    assert client.post("/admin/categories", json={"name": "X"}, headers=auth(cust)).status_code == 403
    assert client.post("/admin/categories", json={"name": "X"}).status_code == 401


def test_menu_availability_toggle(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu_and_table(client, admin)
    menu = client.get("/menu").json()
    assert menu[0]["items"][0]["price"] == 250.0
    client.patch(f"/admin/items/{item['id']}/availability", json={"is_available": False}, headers=auth(admin))
    assert client.get("/menu?only_available=true").json()[0]["items"] == []
    cust = register_customer(client)
    r = client.post("/orders", json={"items": [{"menu_item_id": item["id"], "quantity": 1}]}, headers=auth(cust))
    assert r.status_code == 409


def test_reservation_no_double_booking(client):
    admin = login(client, "admin@example.com", "admin12345")
    setup_menu_and_table(client, admin)  # only 1 table
    cust = register_customer(client)
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    r1 = client.post("/reservations", json={"guests": 2, "start_time": start}, headers=auth(cust))
    assert r1.status_code == 201, r1.text
    r2 = client.post("/reservations", json={"guests": 2, "start_time": start}, headers=auth(cust))
    assert r2.status_code == 409
    avail = client.get("/tables/availability", params={"start_time": start, "guests": 2}).json()
    assert avail == []
    client.post(f"/reservations/{r1.json()['id']}/cancel", headers=auth(cust))
    assert len(client.get("/tables/availability", params={"start_time": start, "guests": 2}).json()) == 1


def test_full_online_payment_flow(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu_and_table(client, admin)
    cust = register_customer(client)
    r = client.post("/orders", json={"items": [{"menu_item_id": item["id"], "quantity": 2}],
                                     "payment_option": "full_online"}, headers=auth(cust))
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["total"] == 500.0 and order["status"] == "pending_payment"
    assert order["payment"]["amount_paise"] == 50000
    rz_id = order["payment"]["razorpay_order_id"]

    bad = client.post("/payments/verify", json={"razorpay_order_id": rz_id, "razorpay_payment_id": "pay_1",
                                                "razorpay_signature": "nope"}, headers=auth(cust))
    assert bad.status_code == 400

    ok = client.post("/payments/verify", json={"razorpay_order_id": rz_id, "razorpay_payment_id": "pay_1",
                                               "razorpay_signature": sign(rz_id, "pay_1")}, headers=auth(cust))
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "placed" and ok.json()["payment_status"] == "paid"

    # verifying twice must not double count
    again = client.post("/payments/verify", json={"razorpay_order_id": rz_id, "razorpay_payment_id": "pay_1",
                                                  "razorpay_signature": sign(rz_id, "pay_1")}, headers=auth(cust))
    assert again.json()["amount_paid"] == 500.0

    # admin kitchen flow
    oid = order["id"]
    assert client.patch(f"/admin/orders/{oid}/status", json={"status": "completed"},
                        headers=auth(admin)).status_code == 409
    for s in ("preparing", "ready", "completed"):
        assert client.patch(f"/admin/orders/{oid}/status", json={"status": s}, headers=auth(admin)).status_code == 200


def test_advance_payment_and_webhook(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu_and_table(client, admin)
    cust = register_customer(client)
    order = client.post("/orders", json={"items": [{"menu_item_id": item["id"], "quantity": 2}],
                                         "payment_option": "advance_online"}, headers=auth(cust)).json()
    assert order["payment"]["amount"] == 250.0  # 50% of 500
    rz_id = order["payment"]["razorpay_order_id"]

    body = json.dumps({"event": "payment.captured",
                       "payload": {"payment": {"entity": {"id": "pay_9", "order_id": rz_id}}}}).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert client.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": "bad"}).status_code == 400
    assert client.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": sig}).status_code == 200

    got = client.get(f"/orders/{order['id']}", headers=auth(cust)).json()
    assert got["status"] == "placed" and got["payment_status"] == "partially_paid" and got["balance"] == 250.0

    paid = client.post(f"/admin/orders/{order['id']}/mark-paid", headers=auth(admin)).json()
    assert paid["payment_status"] == "paid" and paid["balance"] == 0.0


def test_pay_at_pickup_goes_straight_to_placed(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_menu_and_table(client, admin)
    cust = register_customer(client)
    order = client.post("/orders", json={"items": [{"menu_item_id": item["id"], "quantity": 1}],
                                         "payment_option": "pay_at_pickup"}, headers=auth(cust)).json()
    assert order["status"] == "placed" and order["payment"] is None
    # other customers cannot read this order
    other = register_customer(client, "other@example.com")
    assert client.get(f"/orders/{order['id']}", headers=auth(other)).status_code == 404
