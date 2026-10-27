"""Tests for Step 2: CSV Menu Import and Export (Admin only)."""
import codecs
import io
from decimal import Decimal

import pytest


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="cust_import@example.com"):
    r = client.post("/auth/register", json={"name": "Customer", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def test_menu_import_permissions(client):
    cust = register_customer(client)
    sample_csv = "category,name,price,is_veg\nCurries,Paneer,200,yes\n"
    files = {"file": ("menu.csv", io.BytesIO(sample_csv.encode("utf-8")), "text/csv")}

    # Unauthenticated
    assert client.post("/admin/menu/import", files=files).status_code == 401
    assert client.get("/admin/menu/export.csv").status_code == 401

    # Customer
    files = {"file": ("menu.csv", io.BytesIO(sample_csv.encode("utf-8")), "text/csv")}
    assert client.post("/admin/menu/import", files=files, headers=auth(cust)).status_code == 403
    assert client.get("/admin/menu/export.csv", headers=auth(cust)).status_code == 403


def test_menu_import_dry_run_and_commit(client):
    admin = login(client, "admin@example.com", "admin12345")

    csv_data = (
        "category,name,price,is_veg,is_available,description\n"
        "Starters,Crispy Corn,220.00,yes,yes,Spiced crispy sweet corn\n"
        "Mains,Butter Chicken,380.00,no,yes,Rich creamy tomato gravy\n"
    )

    # 1. Dry run = True (preview, nothing saved)
    files = {"file": ("menu.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
    r = client.post("/admin/menu/import?dry_run=true&mode=merge", files=files, headers=auth(admin))
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["dry_run"] is True
    assert report["committed"] is False
    assert report["rows_read"] == 2
    assert report["categories_created"] == 2
    assert report["items_created"] == 2
    assert report["errors"] == []

    # Verify menu is still empty
    assert client.get("/menu").json() == []

    # 2. Dry run = False (committed to DB)
    files = {"file": ("menu.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
    r2 = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files, headers=auth(admin))
    assert r2.status_code == 200, r2.text
    report2 = r2.json()
    assert report2["dry_run"] is False
    assert report2["committed"] is True
    assert report2["categories_created"] == 2
    assert report2["items_created"] == 2

    # Verify menu now has 2 categories and 2 items
    menu = client.get("/menu").json()
    assert len(menu) == 2
    item_names = {item["name"] for cat in menu for item in cat["items"]}
    assert "Crispy Corn" in item_names
    assert "Butter Chicken" in item_names


def test_menu_import_price_formats(client):
    admin = login(client, "admin@example.com", "admin12345")

    csv_data = (
        "category,name,price,is_veg\n"
        "Breads,Plain Naan,₹40,yes\n"
        "Breads,Butter Naan,₹ 50.00,yes\n"
        "Breads,Garlic Naan,Rs.60.00,yes\n"
        "Breads,Cheese Naan,Rs 80,yes\n"
        'Breads,Special Platter,"1,250.00",yes\n'
    )
    files = {"file": ("prices.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
    r = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files, headers=auth(admin))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["items_created"] == 5
    assert rep["errors"] == []

    menu = client.get("/menu").json()
    breads = next(c for c in menu if c["name"] == "Breads")
    prices_by_name = {i["name"]: i["price"] for i in breads["items"]}
    assert prices_by_name["Plain Naan"] == 40.0
    assert prices_by_name["Butter Naan"] == 50.0
    assert prices_by_name["Garlic Naan"] == 60.0
    assert prices_by_name["Cheese Naan"] == 80.0
    assert prices_by_name["Special Platter"] == 1250.0


def test_menu_import_veg_formats_and_required(client):
    admin = login(client, "admin@example.com", "admin12345")

    csv_data = (
        "category,name,price,is_veg\n"
        "Sides,Raita,50,yes\n"
        "Sides,Papad,30,true\n"
        "Sides,Salad,60,1\n"
        "Sides,Pickle,20,veg\n"
        "Sides,Egg Fry,70,no\n"
        "Sides,Chicken Salad,120,false\n"
        "Sides,Fish Finger,150,0\n"
        "Sides,Mutton Seekh,200,non-veg\n"
    )
    files = {"file": ("veg.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
    r = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files, headers=auth(admin))
    assert r.status_code == 200, r.text
    assert r.json()["items_created"] == 8

    # Reject missing or invalid veg flag
    invalid_veg = (
        "category,name,price,is_veg\n"
        "Sides,Unknown Dish,50,\n"
        "Sides,Bad Dish,50,maybe\n"
    )
    files_inv = {"file": ("inv.csv", io.BytesIO(invalid_veg.encode("utf-8")), "text/csv")}
    r_inv = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files_inv, headers=auth(admin))
    assert r_inv.status_code == 200
    rep_inv = r_inv.json()
    assert rep_inv["committed"] is False
    assert len(rep_inv["errors"]) == 2
    assert rep_inv["errors"][0]["field"] == "is_veg"


def test_menu_import_encodings_and_delimiters(client):
    admin = login(client, "admin@example.com", "admin12345")

    # 1. UTF-8 with BOM (Excel UTF-8 CSV)
    csv_str = "category;name;price;is_veg\nBeverages;Sweet Lassi;90.00;yes\n"
    bom_content = codecs.BOM_UTF8 + csv_str.encode("utf-8")
    files_bom = {"file": ("excel.csv", io.BytesIO(bom_content), "text/csv")}
    r_bom = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files_bom, headers=auth(admin))
    assert r_bom.status_code == 200, r_bom.text
    assert r_bom.json()["items_created"] == 1

    # 2. Tab delimiter
    tab_csv = "category\tname\tprice\tis_veg\nDesserts\tGulab Jamun\t100.00\tyes\n"
    files_tab = {"file": ("tab.tsv", io.BytesIO(tab_csv.encode("utf-8")), "text/tab-separated-values")}
    r_tab = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files_tab, headers=auth(admin))
    assert r_tab.status_code == 200, r_tab.text
    assert r_tab.json()["items_created"] == 1

    # 3. Invalid non-UTF-8 bytes rejected with clear message
    bad_bytes = b"category,name,price,is_veg\nStarters,Caf\xe9 Dish,100,yes\n"
    files_bad = {"file": ("bad.csv", io.BytesIO(bad_bytes), "text/csv")}
    r_bad = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files_bad, headers=auth(admin))
    assert r_bad.status_code == 200
    assert "Invalid file encoding" in r_bad.json()["errors"][0]["message"]


def test_menu_import_validation_and_all_or_nothing(client):
    admin = login(client, "admin@example.com", "admin12345")

    # Invalid rows: negative price, zero price, non-numeric price, duplicate inside file
    bad_csv = (
        "category,name,price,is_veg\n"
        "Snacks,Samosa,-10,yes\n"
        "Snacks,Kachori,0,yes\n"
        "Snacks,Pakora,invalid_num,yes\n"
        "Snacks,Samosa,20,yes\n"  # duplicate
        "Snacks,Overprecision,50.123,yes\n"
    )
    files = {"file": ("bad.csv", io.BytesIO(bad_csv.encode("utf-8")), "text/csv")}
    r = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files, headers=auth(admin))
    assert r.status_code == 200
    rep = r.json()
    assert rep["committed"] is False
    assert len(rep["errors"]) == 5
    # Verify no snacks category was created (all-or-nothing)
    cats = [c["name"] for c in client.get("/menu").json()]
    assert "Snacks" not in cats


def test_menu_import_formula_safety(client):
    admin = login(client, "admin@example.com", "admin12345")

    # Cells starting with = + - @
    safe_formula_csv = (
        "category,name,price,is_veg,description\n"
        "Chef Specials,=Special Biryani,300,yes,+Special Secret Spices\n"
    )
    files = {"file": ("formula.csv", io.BytesIO(safe_formula_csv.encode("utf-8")), "text/csv")}
    r = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files, headers=auth(admin))
    assert r.status_code == 200, r.text
    assert r.json()["committed"] is True

    menu = client.get("/menu").json()
    cat = next(c for c in menu if c["name"] == "Chef Specials")
    assert cat["items"][0]["name"] == "=Special Biryani"
    assert cat["items"][0]["description"] == "+Special Secret Spices"


def test_menu_import_replace_mode_and_preserves_order_snapshots(client):
    admin = login(client, "admin@example.com", "admin12345")
    cust = register_customer(client, "order_snapshot_cust@example.com")

    # 1. Initial menu: Dish A and Dish B
    init_csv = (
        "category,name,price,is_veg\n"
        "OldCat,Dish A,100,yes\n"
        "OldCat,Dish B,200,no\n"
    )
    files1 = {"file": ("m1.csv", io.BytesIO(init_csv.encode("utf-8")), "text/csv")}
    client.post("/admin/menu/import?dry_run=false&mode=merge", files=files1, headers=auth(admin))

    menu = client.get("/menu").json()
    dish_a_id = next(i["id"] for cat in menu for i in cat["items"] if i["name"] == "Dish A")

    # 2. Customer places an order with Dish A
    from datetime import datetime, timedelta
    from app.core.timeutils import LOCAL_TZ
    fut_date = (datetime.now(LOCAL_TZ) + timedelta(days=2)).date()
    p_time = datetime(fut_date.year, fut_date.month, fut_date.day, 14, 0, tzinfo=LOCAL_TZ).isoformat()

    r_ord = client.post("/orders", json={
        "order_type": "takeaway",
        "pickup_time": p_time,
        "items": [{"menu_item_id": dish_a_id, "quantity": 2}],
    }, headers=auth(cust))
    assert r_ord.status_code == 201, r_ord.text
    order_id = r_ord.json()["id"]

    # 3. Replace import: only NewCat with Dish C (Dish A and Dish B are removed)
    replace_csv = (
        "category,name,price,is_veg\n"
        "NewCat,Dish C,300,yes\n"
    )
    files_rep = {"file": ("rep.csv", io.BytesIO(replace_csv.encode("utf-8")), "text/csv")}
    r_rep = client.post("/admin/menu/import?dry_run=false&mode=replace", files=files_rep, headers=auth(admin))
    assert r_rep.status_code == 200, r_rep.text
    rep_report = r_rep.json()
    assert rep_report["committed"] is True
    assert rep_report["items_deleted"] == 2
    assert rep_report["categories_deleted"] == 1
    assert rep_report["items_created"] == 1

    # 4. Existing order still readable and retains name and unit_price snapshot
    r_get_ord = client.get(f"/orders/{order_id}", headers=auth(cust))
    assert r_get_ord.status_code == 200
    ord_data = r_get_ord.json()
    assert ord_data["total"] == 200.0
    assert ord_data["items"][0]["name"] == "Dish A"
    assert ord_data["items"][0]["unit_price"] == 100.0
    assert ord_data["items"][0]["menu_item_id"] is None  # FK set null


def test_menu_export_round_trip(client):
    admin = login(client, "admin@example.com", "admin12345")

    # Import menu
    base_csv = (
        "category,name,price,is_veg,is_available,description,image_url\n"
        "Beverages,Mango Lassi,120.00,yes,yes,Thick mango yogurt drink,\n"
        "Mains,Kadai Paneer,310.00,yes,yes,Paneer cooked with bell peppers,\n"
    )
    files = {"file": ("base.csv", io.BytesIO(base_csv.encode("utf-8")), "text/csv")}
    client.post("/admin/menu/import?dry_run=false&mode=replace", files=files, headers=auth(admin))

    # Export CSV
    r_exp = client.get("/admin/menu/export.csv", headers=auth(admin))
    assert r_exp.status_code == 200
    assert "text/csv" in r_exp.headers.get("content-type", "")
    exported_csv = r_exp.text

    # Re-importing that export again must report zero changes
    files_reimport = {"file": ("reimport.csv", io.BytesIO(exported_csv.encode("utf-8")), "text/csv")}
    r_re = client.post("/admin/menu/import?dry_run=false&mode=merge", files=files_reimport, headers=auth(admin))
    assert r_re.status_code == 200
    re_rep = r_re.json()
    assert re_rep["items_created"] == 0
    assert re_rep["items_updated"] == 0
    assert re_rep["items_unchanged"] == 2
    assert re_rep["categories_created"] == 0
    assert re_rep["errors"] == []
