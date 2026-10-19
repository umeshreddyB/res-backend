import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.db import SessionLocal
from app.models import (DiningTable, MenuItem, Order, OrderItem, OrderStatus,
                      OrderType, PaymentOption, PaymentStatus, TableChannel,
                      User)

LOCAL_TZ = ZoneInfo(settings.timezone)


def auth(token: str):
    return {"Authorization": f"Bearer {token}"}


def login(client, email="admin@example.com", password="admin12345"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="report_cust@example.com"):
    r = client.post("/auth/register", json={"name": "CustReport", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_menu_items(client, admin_token):
    cat = client.post("/admin/categories", json={"name": "Reports Menu"}, headers=auth(admin_token)).json()
    item1 = client.post("/admin/items", json={
        "category_id": cat["id"], "name": "Pizza Margherita", "price": "200.00"
    }, headers=auth(admin_token)).json()
    item2 = client.post("/admin/items", json={
        "category_id": cat["id"], "name": "Burger Combo", "price": "100.00"
    }, headers=auth(admin_token)).json()
    item3 = client.post("/admin/items", json={
        "category_id": cat["id"], "name": "Caesar Salad", "price": "50.00"
    }, headers=auth(admin_token)).json()
    return item1, item2, item3


def test_reports_customer_forbidden_and_unauthenticated(client):
    """Customer cannot access reports endpoints (403), unauthenticated gets 401."""
    cust_token = register_customer(client, "forbidden_cust@example.com")

    endpoints = [
        "/admin/reports/summary",
        "/admin/reports/daily",
        "/admin/reports/top-items",
        "/admin/reports/orders.csv",
    ]

    for ep in endpoints:
        # Unauthenticated
        r_unauth = client.get(ep)
        assert r_unauth.status_code == 401

        # Customer -> 403
        r_cust = client.get(ep, headers=auth(cust_token))
        assert r_cust.status_code == 403, f"{ep} did not return 403 for customer"


def test_reports_empty_range(client):
    """Empty date range returns zeroed summaries and empty lists without errors."""
    admin_token = login(client)

    # Pick a date range in 2020 where no orders exist
    params = {"from": "2020-01-01", "to": "2020-01-05"}

    # 1. Summary
    r_sum = client.get("/admin/reports/summary", params=params, headers=auth(admin_token))
    assert r_sum.status_code == 200
    sum_data = r_sum.json()
    assert sum_data["net_sales"] == 0.0
    assert sum_data["gross_paid"] == 0.0
    assert sum_data["total_refunded"] == 0.0
    assert sum_data["orders_count"] == 0
    assert sum_data["cancelled_count"] == 0
    assert sum_data["average_order_value"] == 0.0
    assert sum_data["pending_amount"] == 0.0
    assert sum_data["by_order_type"] == []
    for k, count in sum_data["by_payment_status"].items():
        assert count == 0

    # 2. Daily
    r_daily = client.get("/admin/reports/daily", params=params, headers=auth(admin_token))
    assert r_daily.status_code == 200
    assert r_daily.json() == []

    # 3. Top items
    r_top = client.get("/admin/reports/top-items", params=params, headers=auth(admin_token))
    assert r_top.status_code == 200
    assert r_top.json() == []

    # 4. CSV
    r_csv = client.get("/admin/reports/orders.csv", params=params, headers=auth(admin_token))
    assert r_csv.status_code == 200
    assert r_csv.headers["content-type"].startswith("text/csv")
    lines = r_csv.text.strip().split("\r\n" if "\r\n" in r_csv.text else "\n")
    assert len(lines) == 1  # Only header line
    assert "order_id" in lines[0]


def test_reports_summary_and_daily_sales_with_refunds_and_cancellations(client):
    """Verify net_sales, gross_paid, total_refunded, cancelled orders, and active pending amounts."""
    admin_token = login(client)
    item1, item2, _ = setup_menu_items(client, admin_token)

    today = datetime.now(LOCAL_TZ).date().isoformat()

    with SessionLocal() as db:
        user = db.query(User).first()
        now_dt = datetime.now(timezone.utc)

        # Order 1: takeaway placed and paid in full (500), no refunds
        o1 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("500.00"),
            amount_paid=Decimal("500.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="Alice",
            created_at=now_dt,
        )
        # Order 2: dine_in placed, paid 400, partial refund 100 -> net 300
        o2 = Order(
            user_id=user.id,
            order_type=OrderType.dine_in,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.partially_refunded,
            total=Decimal("400.00"),
            amount_paid=Decimal("400.00"),
            amount_refunded=Decimal("100.00"),
            customer_name="Bob",
            created_at=now_dt,
        )
        # Order 3: order paid 300, then cancelled and fully refunded 300 -> net 0
        o3 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.cancelled,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.refunded,
            total=Decimal("300.00"),
            amount_paid=Decimal("300.00"),
            amount_refunded=Decimal("300.00"),
            customer_name="Charlie",
            created_at=now_dt,
        )
        # Order 4: order cancelled without ever being paid -> excluded from net sales
        o4 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.cancelled,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.unpaid,
            total=Decimal("250.00"),
            amount_paid=Decimal("0.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="Dave",
            created_at=now_dt,
        )
        # Order 5: active order pending_payment (total 200, paid 0) -> pending_amount 200
        o5 = Order(
            user_id=user.id,
            order_type=OrderType.dine_in,
            status=OrderStatus.pending_payment,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.unpaid,
            total=Decimal("200.00"),
            amount_paid=Decimal("0.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="Eve",
            created_at=now_dt,
        )
        db.add_all([o1, o2, o3, o4, o5])
        db.commit()

    # GET /admin/reports/summary for today
    r_sum = client.get("/admin/reports/summary", params={"from": today, "to": today}, headers=auth(admin_token))
    assert r_sum.status_code == 200
    s = r_sum.json()

    # Calculations:
    # gross_paid = 500 (o1) + 400 (o2) + 300 (o3) = 1200.0
    # total_refunded = 100 (o2) + 300 (o3) = 400.0
    # net_sales = 500 (o1) + (400-100) (o2) + (300-300) (o3) = 800.0
    # non-cancelled orders_count = 3 (o1, o2, o5)
    # cancelled_count = 2 (o3, o4)
    # average_order_value = 800.0 / 3 = 266.67
    # pending_amount = 200.0 (o5)
    assert s["gross_paid"] == 1200.0
    assert s["total_refunded"] == 400.0
    assert s["net_sales"] == 800.0
    assert s["orders_count"] == 3
    assert s["cancelled_count"] == 2
    assert s["average_order_value"] == 266.67
    assert s["pending_amount"] == 200.0

    # by_payment_status counts
    pay_counts = s["by_payment_status"]
    assert pay_counts["paid"] == 1
    assert pay_counts["partially_refunded"] == 1
    assert pay_counts["refunded"] == 1
    assert pay_counts["unpaid"] == 2

    # by_order_type
    type_map = {item["order_type"]: item for item in s["by_order_type"]}
    assert type_map["takeaway"]["orders"] == 1  # o1 non-cancelled (o3 and o4 are cancelled)
    assert type_map["takeaway"]["net_sales"] == 500.0  # o1 (500) + o3 (0)
    assert type_map["dine_in"]["orders"] == 2  # o2, o5
    assert type_map["dine_in"]["net_sales"] == 300.0  # o2 (300) + o5 (0)

    # GET /admin/reports/daily for today
    r_daily = client.get("/admin/reports/daily", params={"from": today, "to": today}, headers=auth(admin_token))
    assert r_daily.status_code == 200
    daily_items = r_daily.json()
    assert len(daily_items) == 1
    d = daily_items[0]
    assert d["date"] == today
    assert d["gross_paid"] == 1200.0
    assert d["total_refunded"] == 400.0
    assert d["net_sales"] == 800.0
    assert d["orders_count"] == 3
    assert d["cancelled_count"] == 2


def test_reports_timezone_boundaries(client):
    """Orders placed near midnight (e.g. 23:30 IST) map to the correct local calendar day."""
    admin_token = login(client)

    # Date A: 2026-10-20
    # Date B: 2026-10-21
    # 23:30 IST on 2026-10-20 is 18:00 UTC on 2026-10-20 (IST is UTC + 5:30)
    # 00:30 IST on 2026-10-21 is 19:00 UTC on 2026-10-20
    dt_2330_ist = datetime(2026, 10, 20, 23, 30, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    dt_0030_ist = datetime(2026, 10, 21, 0, 30, tzinfo=LOCAL_TZ).astimezone(timezone.utc)

    with SessionLocal() as db:
        user = db.query(User).first()
        o_day_a = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("150.00"),
            amount_paid=Decimal("150.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="LateNightCustomer",
            created_at=dt_2330_ist,
        )
        o_day_b = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("250.00"),
            amount_paid=Decimal("250.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="EarlyMorningCustomer",
            created_at=dt_0030_ist,
        )
        db.add_all([o_day_a, o_day_b])
        db.commit()

    # Query Date A: 2026-10-20
    r_a = client.get("/admin/reports/summary", params={"from": "2026-10-20", "to": "2026-10-20"}, headers=auth(admin_token))
    assert r_a.status_code == 200
    res_a = r_a.json()
    assert res_a["net_sales"] == 150.0
    assert res_a["orders_count"] == 1

    # Query Date B: 2026-10-21
    r_b = client.get("/admin/reports/summary", params={"from": "2026-10-21", "to": "2026-10-21"}, headers=auth(admin_token))
    assert r_b.status_code == 200
    res_b = r_b.json()
    assert res_b["net_sales"] == 250.0
    assert res_b["orders_count"] == 1

    # Query Daily over both dates
    r_daily = client.get("/admin/reports/daily", params={"from": "2026-10-20", "to": "2026-10-21"}, headers=auth(admin_token))
    assert r_daily.status_code == 200
    days = r_daily.json()
    assert len(days) == 2
    assert days[0]["date"] == "2026-10-20"
    assert days[0]["net_sales"] == 150.0
    assert days[1]["date"] == "2026-10-21"
    assert days[1]["net_sales"] == 250.0


def test_reports_top_items_aggregation_and_cancelled_orders(client):
    """Top items reports aggregate sold quantities and sales, excluding cancelled orders."""
    admin_token = login(client)
    item1, item2, item3 = setup_menu_items(client, admin_token)

    today = datetime.now(LOCAL_TZ).date().isoformat()
    now_dt = datetime.now(timezone.utc)

    with SessionLocal() as db:
        user = db.query(User).first()

        # Order 1 (placed): 3 Pizzas @ 200 = 600, 1 Burger @ 100 = 100
        o1 = Order(
            user_id=user.id,
            order_type=OrderType.dine_in,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("700.00"),
            amount_paid=Decimal("700.00"),
            customer_name="Customer 1",
            created_at=now_dt,
        )
        db.add(o1)
        db.flush()
        db.add(OrderItem(order_id=o1.id, menu_item_id=item1["id"], name=item1["name"], unit_price=Decimal("200.00"), quantity=3))
        db.add(OrderItem(order_id=o1.id, menu_item_id=item2["id"], name=item2["name"], unit_price=Decimal("100.00"), quantity=1))

        # Order 2 (placed): 2 Burgers @ 100 = 200
        o2 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("200.00"),
            amount_paid=Decimal("200.00"),
            customer_name="Customer 2",
            created_at=now_dt,
        )
        db.add(o2)
        db.flush()
        db.add(OrderItem(order_id=o2.id, menu_item_id=item2["id"], name=item2["name"], unit_price=Decimal("100.00"), quantity=2))

        # Order 3 (cancelled): 10 Salads @ 50 = 500 (must be excluded!)
        o3 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.cancelled,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.unpaid,
            total=Decimal("500.00"),
            amount_paid=Decimal("0.00"),
            customer_name="Customer 3",
            created_at=now_dt,
        )
        db.add(o3)
        db.flush()
        db.add(OrderItem(order_id=o3.id, menu_item_id=item3["id"], name=item3["name"], unit_price=Decimal("50.00"), quantity=10))

        db.commit()

    # GET /admin/reports/top-items?limit=2
    r_top = client.get("/admin/reports/top-items", params={"from": today, "to": today, "limit": 2}, headers=auth(admin_token))
    assert r_top.status_code == 200
    items = r_top.json()
    assert len(items) == 2

    # Item 1 should be Pizza (quantity 3, net_sales 600)
    # Item 2 should be Burger (quantity 3, net_sales 300)
    # Salad (quantity 10 in cancelled order) is completely excluded!
    assert items[0]["item_name"] == "Pizza Margherita"
    assert items[0]["quantity_sold"] == 3
    assert items[0]["net_sales"] == 600.0

    assert items[1]["item_name"] == "Burger Combo"
    assert items[1]["quantity_sold"] == 3
    assert items[1]["net_sales"] == 300.0


def test_reports_orders_csv_and_formula_injection_protection(client):
    """CSV export includes all requested columns and escapes formula injection characters."""
    admin_token = login(client)
    today = datetime.now(LOCAL_TZ).date().isoformat()
    now_dt = datetime.now(timezone.utc)

    with SessionLocal() as db:
        user = db.query(User).first()
        t1 = DiningTable(number=12, capacity=4, channel=TableChannel.own_app)
        db.add(t1)
        db.flush()

        # Malicious names starting with '=', '+', '-', '@'
        o1 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="=SUM(A1:B1)",
            customer_phone="+919876543210",
            created_at=now_dt,
        )
        o2 = Order(
            user_id=user.id,
            order_type=OrderType.takeaway,
            status=OrderStatus.placed,
            payment_option=PaymentOption.full_online,
            payment_status=PaymentStatus.paid,
            total=Decimal("150.00"),
            amount_paid=Decimal("150.00"),
            amount_refunded=Decimal("0.00"),
            customer_name="@admin",
            customer_phone="-12345",
            created_at=now_dt,
        )
        db.add_all([o1, o2])
        db.commit()

    r = client.get("/admin/reports/orders.csv", params={"from": today, "to": today}, headers=auth(admin_token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"orders_{today}_{today}.csv" in r.headers["content-disposition"]

    csv_reader = csv.reader(io.StringIO(r.text))
    rows = list(csv_reader)

    expected_headers = [
        "order_id", "created_at", "order_type", "table_number", "status",
        "payment_status", "total_amount", "amount_paid", "amount_refunded",
        "net_amount", "customer_name", "customer_phone"
    ]
    assert rows[0] == expected_headers
    assert len(rows) == 3

    # Check escaped cells
    row1 = rows[1]
    name_col = expected_headers.index("customer_name")
    phone_col = expected_headers.index("customer_phone")

    assert row1[name_col] == "'=SUM(A1:B1)"
    assert row1[phone_col] == "'+919876543210"

    row2 = rows[2]
    assert row2[name_col] == "'@admin"
    assert row2[phone_col] == "'-12345"


def test_reports_validation_errors(client):
    """Validation handles bad date formats, inverted dates, and ranges > 366 days."""
    admin_token = login(client)

    # 1. from > to -> 400
    r1 = client.get("/admin/reports/summary", params={"from": "2026-10-15", "to": "2026-10-10"}, headers=auth(admin_token))
    assert r1.status_code == 400
    assert "cannot be after" in r1.json()["detail"].lower()

    # 2. invalid date format -> 400
    r2 = client.get("/admin/reports/summary", params={"from": "not-a-date"}, headers=auth(admin_token))
    assert r2.status_code == 400
    assert "invalid" in r2.json()["detail"].lower()

    # 3. range > 366 days -> 400
    r3 = client.get("/admin/reports/summary", params={"from": "2024-01-01", "to": "2025-02-01"}, headers=auth(admin_token))
    assert r3.status_code == 400
    assert "366" in r3.json()["detail"]
