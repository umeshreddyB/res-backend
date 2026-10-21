import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.db import SessionLocal
from app.models import Order, Reservation, ReservationStatus, TableChannel


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def sign(order_id, payment_id, secret="test-secret"):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def register_customer(client, email="cust_br@example.com"):
    r = client.post("/auth/register", json={"name": "CustBR", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_data(client, admin):
    cat = client.post("/admin/categories", json={"name": "Specialties"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"], "name": "Biryani Bowl", "price": "300.00"
    }, headers=auth(admin)).json()
    t1 = client.post("/admin/tables", json={"number": 1, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()
    return item, t1


def slot_time(days_ahead=2, hour=14):
    return (datetime.now(timezone.utc) + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_dine_in_creates_held_reservation_and_payment_verifies(client):
    """dine-in creates a held reservation and the order together; payment verify confirms both."""
    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust = register_customer(client, "dine_cust1@example.com")

    slot = slot_time()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": slot,
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    r = client.post("/orders", json=payload, headers=auth(cust))
    assert r.status_code == 201, r.text
    order = r.json()

    assert order["status"] == "pending_payment"
    assert order["table_number"] == 1
    assert order["reservation_id"] is not None

    with SessionLocal() as db:
        res = db.get(Reservation, order["reservation_id"])
        assert res is not None
        assert res.status == ReservationStatus.held
        assert res.hold_expires_at is not None
        assert res.table_id == table["id"]

    rz_id = order["payment"]["razorpay_order_id"]
    sig = sign(rz_id, "pay_verify1")
    v = client.post("/payments/verify", json={
        "razorpay_order_id": rz_id,
        "razorpay_payment_id": "pay_verify1",
        "razorpay_signature": sig
    }, headers=auth(cust))
    assert v.status_code == 200, v.text
    assert v.json()["status"] == "placed"
    assert v.json()["payment_status"] == "paid"

    with SessionLocal() as db:
        res = db.get(Reservation, order["reservation_id"])
        assert res.status == ReservationStatus.confirmed
        assert res.hold_expires_at is None


def test_second_dine_in_same_slot_returns_409_and_creates_nothing(client):
    """a second dine-in for the same table and slot returns 409 and creates nothing."""
    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust1 = register_customer(client, "dine_r1@example.com")
    cust2 = register_customer(client, "dine_r2@example.com")

    slot = slot_time()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": slot,
        "table_id": table["id"],
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    r1 = client.post("/orders", json=payload, headers=auth(cust1))
    assert r1.status_code == 201, r1.text

    # Second dine-in for same slot and table
    r2 = client.post("/orders", json=payload, headers=auth(cust2))
    assert r2.status_code == 409
    assert "No table is free" in r2.json()["detail"]

    # Verify no second order or reservation exists
    with SessionLocal() as db:
        orders = db.query(Order).filter(Order.user_id != 1).all()
        assert len(orders) == 1
        reservations = db.query(Reservation).all()
        assert len(reservations) == 1


def test_unpaid_dine_in_releases_table_after_timeout(client):
    """an unpaid dine-in releases its table after the timeout and the table can be booked again."""
    from app.services.abandoned_orders import cancel_abandoned_orders

    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust1 = register_customer(client, "expire1@example.com")
    cust2 = register_customer(client, "expire2@example.com")

    slot = slot_time()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": slot,
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    r1 = client.post("/orders", json=payload, headers=auth(cust1))
    assert r1.status_code == 201
    order1 = r1.json()

    # Expire the hold
    with SessionLocal() as db:
        res = db.get(Reservation, order1["reservation_id"])
        res.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()

    # Run cleanup
    cancelled_count = cancel_abandoned_orders()
    assert cancelled_count == 1

    with SessionLocal() as db:
        o1 = db.get(Order, order1["id"])
        assert o1.status.value == "cancelled"
        r1_db = db.get(Reservation, order1["reservation_id"])
        assert r1_db.status.value == "cancelled"

    # Now customer 2 can book the same slot
    r2 = client.post("/orders", json=payload, headers=auth(cust2))
    assert r2.status_code == 201, r2.text
    assert r2.json()["table_number"] == 1


def test_late_payment_on_expired_hold_reconfirms_if_free(client):
    """late payment on an expired hold reconfirms the table if it's still free."""
    from app.services.abandoned_orders import cancel_abandoned_orders

    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust = register_customer(client, "late1@example.com")

    slot = slot_time()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": slot,
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    r = client.post("/orders", json=payload, headers=auth(cust))
    order = r.json()
    rz_id = order["payment"]["razorpay_order_id"]

    # Expire hold and cancel order
    with SessionLocal() as db:
        res = db.get(Reservation, order["reservation_id"])
        res.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

    cancel_abandoned_orders()

    # Late payment arrives via webhook
    body = json.dumps({"event": "payment.captured",
                       "payload": {"payment": {"entity": {"id": "pay_late_free", "order_id": rz_id}}}}).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    client.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": sig})

    # Since table was free, reservation is reconfirmed and order placed
    with SessionLocal() as db:
        res = db.get(Reservation, order["reservation_id"])
        assert res.status == ReservationStatus.confirmed
        o = db.get(Order, order["id"])
        assert o.status.value == "placed"
        assert o.needs_attention is False


def test_late_payment_on_expired_hold_does_not_double_book_if_taken(client):
    """late payment when table has been taken keeps order cancelled and flags needs_attention."""
    from app.services.abandoned_orders import cancel_abandoned_orders

    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust1 = register_customer(client, "late_taken1@example.com")
    cust2 = register_customer(client, "late_taken2@example.com")

    slot = slot_time()
    payload = {
        "order_type": "dine_in",
        "guests": 2,
        "pickup_time": slot,
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }

    order1 = client.post("/orders", json=payload, headers=auth(cust1)).json()
    rz_id1 = order1["payment"]["razorpay_order_id"]

    # Expire and cancel order 1
    with SessionLocal() as db:
        res = db.get(Reservation, order1["reservation_id"])
        res.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    cancel_abandoned_orders()

    # Customer 2 books the same table
    order2 = client.post("/orders", json=payload, headers=auth(cust2)).json()
    rz_id2 = order2["payment"]["razorpay_order_id"]
    client.post("/payments/verify", json={
        "razorpay_order_id": rz_id2, "razorpay_payment_id": "pay_taken2",
        "razorpay_signature": sign(rz_id2, "pay_taken2")
    }, headers=auth(cust2))

    # Now late payment arrives for customer 1
    body = json.dumps({"event": "payment.captured",
                       "payload": {"payment": {"entity": {"id": "pay_late_taken1", "order_id": rz_id1}}}}).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    client.post("/payments/webhook", content=body, headers={"X-Razorpay-Signature": sig})

    with SessionLocal() as db:
        o1 = db.get(Order, order1["id"])
        assert o1.status.value == "cancelled"
        assert o1.amount_paid == 300.0
        assert o1.needs_attention is True
        res1 = db.get(Reservation, order1["reservation_id"])
        assert res1.status == ReservationStatus.cancelled


def test_payment_option_and_pickup_time_validation(client):
    """takeaway without pickup_time is rejected; takeaway and dine-in with advance_online or pay_at_pickup return 400."""
    admin = login(client, "admin@example.com", "admin12345")
    item, table = setup_data(client, admin)
    cust = register_customer(client, "val_cust@example.com")

    slot = slot_time()

    # 1. Takeaway without pickup_time -> 400
    r = client.post("/orders", json={
        "order_type": "takeaway",
        "payment_option": "full_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400
    assert "pickup_time is required" in r.json()["detail"]

    # 2. Takeaway with advance_online -> 400
    r = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": slot,
        "payment_option": "advance_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400
    assert "full_online" in r.json()["detail"]

    # 3. Takeaway with pay_at_pickup -> 400
    r = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": slot,
        "payment_option": "pay_at_pickup",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400
    assert "full_online" in r.json()["detail"]

    # 4. Dine-in with advance_online -> 400
    r = client.post("/orders", json={
        "order_type": "dine_in",
        "pickup_time": slot,
        "guests": 2,
        "payment_option": "advance_online",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400

    # 5. Dine-in with pay_at_pickup -> 400
    r = client.post("/orders", json={
        "order_type": "dine_in",
        "pickup_time": slot,
        "guests": 2,
        "payment_option": "pay_at_pickup",
        "items": [{"menu_item_id": item["id"], "quantity": 1}],
    }, headers=auth(cust))
    assert r.status_code == 400


def test_table_channels_availability_and_auto_assignment(client):
    """tables with channel swiggy or walk_in never appear in /tables/availability and are never auto-assigned."""
    admin = login(client, "admin@example.com", "admin12345")
    cat = client.post("/admin/categories", json={"name": "Cat"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={"category_id": cat["id"], "name": "Food", "price": "100.00"}, headers=auth(admin)).json()

    # Create tables across channels
    t_own = client.post("/admin/tables", json={"number": 20, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()
    t_swiggy = client.post("/admin/tables", json={"number": 21, "capacity": 4, "channel": "swiggy"}, headers=auth(admin)).json()
    t_walkin = client.post("/admin/tables", json={"number": 22, "capacity": 4, "channel": "walk_in"}, headers=auth(admin)).json()

    slot = slot_time()
    avail = client.get("/tables/availability", params={"start_time": slot, "guests": 2}).json()
    table_numbers = [t["number"] for t in avail]
    assert 20 in table_numbers
    assert 21 not in table_numbers
    assert 22 not in table_numbers

    # Book the own_app table
    cust = register_customer(client, "chan_cust@example.com")
    res = client.post("/reservations", json={"guests": 2, "start_time": slot}, headers=auth(cust))
    assert res.status_code == 201
    assert res.json()["table"]["number"] == 20

    # Auto-assignment when own_app is full returns 409 (never falls back to swiggy/walk_in)
    cust2 = register_customer(client, "chan_cust2@example.com")
    res2 = client.post("/reservations", json={"guests": 2, "start_time": slot}, headers=auth(cust2))
    assert res2.status_code == 409


def test_admin_manual_booking_on_swiggy_table_blocks_availability(client):
    """an admin manual booking on a swiggy table works and blocks our availability for that slot."""
    admin = login(client, "admin@example.com", "admin12345")
    # Table 30 is swiggy
    t_swiggy = client.post("/admin/tables", json={"number": 30, "capacity": 4, "channel": "swiggy"}, headers=auth(admin)).json()

    slot = slot_time()
    # Admin manual booking on swiggy table
    r = client.post("/admin/reservations", json={
        "table_id": t_swiggy["id"],
        "guests": 3,
        "start_time": slot,
        "source": "swiggy",
        "customer_name": "Swiggy Guest",
        "customer_phone": "+919876543210",
        "notes": "Order #SWIGGY123"
    }, headers=auth(admin))
    assert r.status_code == 201, r.text
    res = r.json()
    assert res["source"] == "swiggy"
    assert res["status"] == "confirmed"
    assert res["customer_name"] == "Swiggy Guest"

    # Second booking for same table and slot is rejected
    r2 = client.post("/admin/reservations", json={
        "table_id": t_swiggy["id"],
        "guests": 2,
        "start_time": slot,
        "source": "phone",
    }, headers=auth(admin))
    assert r2.status_code == 409


def test_changing_table_channel_with_future_bookings_rejected(client):
    """changing a table's channel with future bookings is rejected with 409."""
    admin = login(client, "admin@example.com", "admin12345")
    t = client.post("/admin/tables", json={"number": 40, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()

    slot = slot_time()
    cust = register_customer(client, "channel_change_cust@example.com")
    # Future reservation
    r = client.post("/reservations", json={"guests": 2, "start_time": slot, "table_id": t["id"]}, headers=auth(cust))
    assert r.status_code == 201

    # Attempt to change channel
    patch_res = client.patch(f"/admin/tables/{t['id']}", json={"channel": "swiggy"}, headers=auth(admin))
    assert patch_res.status_code == 409
    assert "Cannot change table channel" in patch_res.json()["detail"]


def test_admin_booking_now_succeeds_and_blocks_availability(client):
    """Admin can book 'now' (or within 12h past for walk-in/counter), blocking table availability."""
    from app.core.timeutils import LOCAL_TZ

    admin = login(client, "admin@example.com", "admin12345")
    t = client.post("/admin/tables", json={"number": 51, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()

    # Admin books a slot 5 minutes ahead (or right now)
    target_dt = (datetime.now(LOCAL_TZ) + timedelta(minutes=5)).replace(microsecond=0)
    target_iso = target_dt.isoformat()

    r_admin = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": target_iso,
        "source": "walk_in",
        "customer_name": "Walk-in Guest",
    }, headers=auth(admin))
    assert r_admin.status_code == 201, r_admin.text
    res = r_admin.json()
    assert res["status"] == "confirmed"

    # Availability for this slot or overlapping slot shows table 51 is blocked
    # Check if target is in operating hours, table 51 must not be returned
    r_avail = client.get(f"/tables/availability?start_time={target_iso}&guests=2")
    if r_avail.status_code == 200:
        table_ids = [tbl["id"] for tbl in r_avail.json()]
        assert t["id"] not in table_ids, "Booked table must be blocked in availability"

    # Overlapping admin booking on same table returns 409
    r_overlap = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": target_iso,
        "source": "phone",
    }, headers=auth(admin))
    assert r_overlap.status_code == 409


def test_admin_booking_outside_opening_hours_succeeds_customer_rejected_and_overlap_409(client):
    """Admin manual bookings skip operating-hours checks; customer bookings for same slot are rejected; overlap returns 409."""
    from app.core.timeutils import LOCAL_TZ

    admin = login(client, "admin@example.com", "admin12345")
    t = client.post("/admin/tables", json={"number": 52, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()
    cust = register_customer(client, "outside_hours_cust@example.com")

    # Pick 03:30 AM local time 2 days from now (restaurant hours are 11:00 to 23:00)
    future_date = (datetime.now(LOCAL_TZ) + timedelta(days=2)).date()
    outside_hours_local = datetime(future_date.year, future_date.month, future_date.day, 3, 30, tzinfo=LOCAL_TZ)
    outside_hours_iso = outside_hours_local.isoformat()

    # 1. Customer attempts to book outside opening hours -> rejected with 400
    r_cust = client.post("/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": outside_hours_iso,
    }, headers=auth(cust))
    assert r_cust.status_code == 400
    assert "operating hours" in r_cust.text.lower() or "closed" in r_cust.text.lower()

    # 2. Admin books that exact same slot outside opening hours -> succeeds (201)
    r_admin = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": outside_hours_iso,
        "source": "phone",
        "customer_name": "VIP Midnight Party",
    }, headers=auth(admin))
    assert r_admin.status_code == 201, r_admin.text
    assert r_admin.json()["status"] == "confirmed"

    # 3. Overlap on that table returns 409
    r_overlap = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": outside_hours_iso,
        "source": "walk_in",
    }, headers=auth(admin))
    assert r_overlap.status_code == 409


def test_admin_booking_past_limits(client):
    """Admin can book up to 12 hours in the past; >12h past is rejected; customer cannot book past."""
    admin = login(client, "admin@example.com", "admin12345")
    t = client.post("/admin/tables", json={"number": 53, "capacity": 4, "channel": "own_app"}, headers=auth(admin)).json()
    cust = register_customer(client, "past_limit_cust@example.com")

    now = datetime.now(timezone.utc)
    two_hours_ago = (now - timedelta(hours=2)).isoformat()
    thirteen_hours_ago = (now - timedelta(hours=13)).isoformat()

    # Customer booking past -> 400
    r_cust = client.post("/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": two_hours_ago,
    }, headers=auth(cust))
    assert r_cust.status_code == 400
    assert "future" in r_cust.text.lower()

    # Admin booking >12h ago -> 400
    r_too_old = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": thirteen_hours_ago,
    }, headers=auth(admin))
    assert r_too_old.status_code == 400
    assert "12 hours" in r_too_old.text.lower()

    # Admin booking 2h ago -> succeeds 201
    r_ok = client.post("/admin/reservations", json={
        "table_id": t["id"],
        "guests": 2,
        "start_time": two_hours_ago,
        "source": "phone",
    }, headers=auth(admin))
    assert r_ok.status_code == 201
