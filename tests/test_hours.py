"""Tests for Task 6: Operating Hours, Restaurant Settings, and Reservation Validation."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.timeutils import LOCAL_TZ


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="cust_hours@example.com"):
    r = client.post("/auth/register", json={"name": "Cust", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_table(client, admin):
    client.post("/admin/tables", json={"number": 10, "capacity": 6}, headers=auth(admin))


def get_next_weekday(weekday: int, hour: int = 19, minute: int = 0) -> datetime:
    """Returns a datetime in Asia/Kolkata for the next occurrence of `weekday` (0=Mon, 6=Sun) at hour:minute."""
    now_local = datetime.now(LOCAL_TZ)
    days_ahead = (weekday - now_local.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7  # ensure next week so it is strictly in the future
    target_date = (now_local + timedelta(days=days_ahead)).date()
    return datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=LOCAL_TZ)


def test_public_and_admin_get_hours(client):
    r = client.get("/settings/hours")
    assert r.status_code == 200
    data = r.json()
    assert "rules" in data and "hours" in data
    assert len(data["hours"]) == 7
    assert data["rules"]["reservation_duration_minutes"] == 90
    assert data["rules"]["max_party_size"] == 20

    admin = login(client, "admin@example.com", "admin12345")
    r_admin = client.get("/admin/settings/hours", headers=auth(admin))
    assert r_admin.status_code == 200
    assert r_admin.json() == data


def test_customer_cannot_update_hours(client):
    cust = register_customer(client)
    r = client.put("/admin/settings/hours", json={"rules": {"max_party_size": 10}}, headers=auth(cust))
    assert r.status_code == 403


def test_admin_update_hours_and_rules(client):
    admin = login(client, "admin@example.com", "admin12345")
    payload = {
        "rules": {
            "reservation_duration_minutes": 60,
            "last_reservation_buffer_minutes": 30,
            "max_party_size": 10,
            "max_days_ahead": 14,
            "advance_percent": 50,
        },
        "hours": [
            {"day_of_week": 1, "is_closed": True},  # Tuesday closed
            {"day_of_week": 4, "is_closed": False, "open_time": "18:00:00", "close_time": "01:00:00"},  # Friday past midnight
        ]
    }
    r = client.put("/admin/settings/hours", json=payload, headers=auth(admin))
    assert r.status_code == 200
    res = r.json()
    assert res["rules"]["max_party_size"] == 10
    assert res["rules"]["reservation_duration_minutes"] == 60

    hours_map = {h["day_of_week"]: h for h in res["hours"]}
    assert hours_map[1]["is_closed"] is True
    assert hours_map[4]["open_time"] == "18:00:00"
    assert hours_map[4]["close_time"] == "01:00:00"


def test_reservation_rules_party_size_and_days_ahead(client):
    admin = login(client, "admin@example.com", "admin12345")
    setup_table(client, admin)
    cust = register_customer(client)

    # Set max party size = 4, max days ahead = 7
    client.put("/admin/settings/hours", json={
        "rules": {
            "max_party_size": 4,
            "max_days_ahead": 7,
            "reservation_duration_minutes": 90,
            "last_reservation_buffer_minutes": 60,
        }
    }, headers=auth(admin))

    # Party size 5 exceeds max party size 4
    slot = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0).isoformat()
    r = client.post("/reservations", json={"guests": 5, "start_time": slot}, headers=auth(cust))
    assert r.status_code == 400
    assert "party size" in r.text.lower()

    # Slot 10 days ahead exceeds max days ahead 7
    far_slot = (datetime.now(timezone.utc) + timedelta(days=10)).replace(hour=13, minute=0, second=0, microsecond=0).isoformat()
    r_far = client.post("/reservations", json={"guests": 2, "start_time": far_slot}, headers=auth(cust))
    assert r_far.status_code == 400
    assert "days in advance" in r_far.text.lower()


def test_reservation_on_closed_day(client):
    admin = login(client, "admin@example.com", "admin12345")
    setup_table(client, admin)
    cust = register_customer(client)

    # Close Mondays (day 0)
    client.put("/admin/settings/hours", json={
        "hours": [{"day_of_week": 0, "is_closed": True}]
    }, headers=auth(admin))

    next_monday = get_next_weekday(0, hour=13, minute=0)
    r = client.post("/reservations", json={"guests": 2, "start_time": next_monday.isoformat()}, headers=auth(cust))
    assert r.status_code == 400
    assert "closed on mondays" in r.text.lower()

    # Table availability also reflects closed day
    r_avail = client.get("/tables/availability", params={"guests": 2, "start_time": next_monday.isoformat()})
    assert r_avail.status_code == 400


def test_overnight_hours_past_midnight(client):
    """Test Friday open 18:00 to 01:00 (Saturday morning).
    Duration: 90 min, Buffer: 60 min.
    Latest valid reservation start = 01:00 - 90 min = 23:30.
    """
    admin = login(client, "admin@example.com", "admin12345")
    setup_table(client, admin)
    cust = register_customer(client)

    client.put("/admin/settings/hours", json={
        "rules": {
            "reservation_duration_minutes": 90,
            "last_reservation_buffer_minutes": 60,
            "max_party_size": 10,
            "max_days_ahead": 30,
        },
        "hours": [
            {"day_of_week": 4, "is_closed": False, "open_time": "18:00:00", "close_time": "01:00:00"}
        ]
    }, headers=auth(admin))

    # 1. Friday 20:00 local time -> valid
    fri_20 = get_next_weekday(4, hour=20, minute=0)
    r1 = client.post("/reservations", json={"guests": 2, "start_time": fri_20.isoformat()}, headers=auth(cust))
    assert r1.status_code == 201, r1.text

    # 2. Friday 23:30 local time -> ends at 01:00 Saturday -> valid
    fri_2330 = get_next_weekday(4, hour=23, minute=30)
    r2 = client.post("/reservations", json={"guests": 2, "start_time": fri_2330.isoformat()}, headers=auth(cust))
    assert r2.status_code == 201, r2.text

    # 3. Friday 23:45 local time -> ends at 01:15 Saturday (past 01:00 close) -> invalid
    fri_2345 = get_next_weekday(4, hour=23, minute=45)
    r3 = client.post("/reservations", json={"guests": 2, "start_time": fri_2345.isoformat()}, headers=auth(cust))
    assert r3.status_code == 400
    assert "operating hours" in r3.text.lower()

    # 4. Saturday 00:15 (next calendar day during Friday overnight shift)
    # Ends at 01:45 (past 01:00 close) -> invalid
    sat_0015 = fri_20 + timedelta(hours=4, minutes=15)
    r4 = client.post("/reservations", json={"guests": 2, "start_time": sat_0015.isoformat()}, headers=auth(cust))
    assert r4.status_code == 400
    assert "operating hours" in r4.text.lower()


def test_timezone_conversion_utc_to_kolkata(client):
    """A user sends a UTC timestamp. It must be correctly mapped to Asia/Kolkata (UTC+5:30).
    For example: Friday 14:00 UTC is Friday 19:30 IST (during Friday 11:00-23:00 IST open hours).
    Whereas Friday 03:00 UTC is Friday 08:30 IST (before restaurant opens at 11:00 IST).
    """
    admin = login(client, "admin@example.com", "admin12345")
    setup_table(client, admin)
    cust = register_customer(client)

    # Ensure Friday is standard 11:00 to 23:00 IST
    client.put("/admin/settings/hours", json={
        "hours": [{"day_of_week": 4, "is_closed": False, "open_time": "11:00:00", "close_time": "23:00:00"}]
    }, headers=auth(admin))

    fri_ist = get_next_weekday(4, hour=19, minute=30)  # 19:30 IST = 14:00 UTC
    fri_utc = fri_ist.astimezone(timezone.utc)

    # 1. 14:00 UTC = 19:30 IST -> should succeed
    r_ok = client.post("/reservations", json={"guests": 2, "start_time": fri_utc.isoformat()}, headers=auth(cust))
    assert r_ok.status_code == 201

    # 2. 03:00 UTC = 08:30 IST (closed, opens at 11:00 IST) -> should fail 400
    early_utc = (fri_ist.replace(hour=8, minute=30)).astimezone(timezone.utc)
    r_early = client.post("/reservations", json={"guests": 2, "start_time": early_utc.isoformat()}, headers=auth(cust))
    assert r_early.status_code == 400
    assert "operating hours" in r_early.text.lower()
