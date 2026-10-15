"""Tests for Task 7: Menu Item Image Uploads, Magic Bytes Validation, and Storage."""
import io
from pathlib import Path

from app.core.config import settings


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def register_customer(client, email="cust_img@example.com"):
    r = client.post("/auth/register", json={"name": "Cust", "email": email, "password": "password123"})
    assert r.status_code == 201, r.text
    return login(client, email, "password123")


def setup_item(client, admin):
    cat = client.post("/admin/categories", json={"name": "Desserts"}, headers=auth(admin)).json()
    item = client.post("/admin/items", json={
        "category_id": cat["id"],
        "name": "Gulab Jamun",
        "price": "120.00"
    }, headers=auth(admin)).json()
    return item


def test_upload_valid_jpeg(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)

    # Valid JPEG header
    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"extra_payload_data"
    files = {"file": ("my_custom_photo.jpg", io.BytesIO(fake_jpeg), "image/jpeg")}

    r = client.post(f"/admin/items/{item['id']}/image", files=files, headers=auth(admin))
    assert r.status_code == 200, r.text
    data = r.json()
    image_url = data["image_url"]
    assert image_url.startswith("/media/")
    assert image_url.endswith(".jpg")

    # Filename must NOT be the uploaded filename (must be random UUID)
    filename = image_url.replace("/media/", "")
    assert filename != "my_custom_photo.jpg"
    assert len(filename) > 30

    # Ensure file is saved and accessible via static route
    r_media = client.get(image_url)
    assert r_media.status_code == 200
    assert r_media.content == fake_jpeg


def test_upload_valid_png_and_webp(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)

    # Valid PNG header
    fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    r_png = client.post(
        f"/admin/items/{item['id']}/image",
        files={"file": ("photo.png", io.BytesIO(fake_png), "image/png")},
        headers=auth(admin)
    )
    assert r_png.status_code == 200
    assert r_png.json()["image_url"].endswith(".png")

    # Valid WebP header
    fake_webp = b"RIFF\x18\x00\x00\x00WEBPVP8 \x0c\x00\x00\x00"
    r_webp = client.post(
        f"/admin/items/{item['id']}/image",
        files={"file": ("photo.webp", io.BytesIO(fake_webp), "image/webp")},
        headers=auth(admin)
    )
    assert r_webp.status_code == 200
    assert r_webp.json()["image_url"].endswith(".webp")


def test_reject_fake_image_by_magic_bytes(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)

    # Disguised as .jpg by extension and Content-Type, but content is arbitrary text/script
    fake_file = b"<!DOCTYPE html><html><body><h1>Not an image</h1></body></html>"
    files = {"file": ("innocent.jpg", io.BytesIO(fake_file), "image/jpeg")}

    r = client.post(f"/admin/items/{item['id']}/image", files=files, headers=auth(admin))
    assert r.status_code == 400
    assert "invalid image file" in r.text.lower()


def test_customer_cannot_upload_or_delete_image(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)
    cust = register_customer(client)

    fake_jpeg = b"\xff\xd8\xff\xe0" + b"data"
    files = {"file": ("img.jpg", io.BytesIO(fake_jpeg), "image/jpeg")}

    r_post = client.post(f"/admin/items/{item['id']}/image", files=files, headers=auth(cust))
    assert r_post.status_code == 403

    r_del = client.delete(f"/admin/items/{item['id']}/image", headers=auth(cust))
    assert r_del.status_code == 403


def test_replace_and_delete_image_cleans_up_disk(client):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)

    # Upload first image
    img1 = b"\xff\xd8\xff\xe0" + b"image_one"
    r1 = client.post(f"/admin/items/{item['id']}/image",
                     files={"file": ("img1.jpg", io.BytesIO(img1), "image/jpeg")},
                     headers=auth(admin))
    url1 = r1.json()["image_url"]
    file1 = Path(settings.media_dir) / url1.replace("/media/", "")
    assert file1.exists()

    # Upload second image (replaces first)
    img2 = b"\x89PNG\r\n\x1a\n" + b"image_two"
    r2 = client.post(f"/admin/items/{item['id']}/image",
                     files={"file": ("img2.png", io.BytesIO(img2), "image/png")},
                     headers=auth(admin))
    url2 = r2.json()["image_url"]
    file2 = Path(settings.media_dir) / url2.replace("/media/", "")
    assert file2.exists()
    assert not file1.exists(), "Old image file should have been deleted when replaced"

    # Delete image endpoint
    r_del = client.delete(f"/admin/items/{item['id']}/image", headers=auth(admin))
    assert r_del.status_code == 200
    assert r_del.json()["image_url"] is None
    assert not file2.exists(), "Image file should be deleted from disk on delete endpoint"


def test_upload_exceeds_max_size(client, monkeypatch):
    admin = login(client, "admin@example.com", "admin12345")
    item = setup_item(client, admin)

    # Temporarily set max upload to 1MB for testing
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)

    oversized = b"\xff\xd8\xff\xe0" + (b"x" * (1024 * 1024 + 100))
    files = {"file": ("big.jpg", io.BytesIO(oversized), "image/jpeg")}

    r = client.post(f"/admin/items/{item['id']}/image", files=files, headers=auth(admin))
    assert r.status_code == 413
    assert "exceeds maximum allowed size" in r.text.lower()
