"""Tests for Task 9: Production Readiness (Health check, Request IDs, Consistent Errors, Trusted Proxies)."""
from unittest.mock import MagicMock

from fastapi import Request
from starlette.datastructures import Address

from app.core.config import settings
from app.core.rate_limit import get_client_ip, is_trusted_proxy


def test_health_check_healthy(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert "request_id" in data
    assert "X-Request-ID" in r.headers


def test_health_check_unhealthy(client, monkeypatch):
    from app import main

    class BrokenSession:
        def __enter__(self):
            raise RuntimeError("Database connection refused")

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(main, "SessionLocal", BrokenSession)
    r = client.get("/health")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "unhealthy"
    assert data["database"] == "unavailable"
    assert "request_id" in data["error"]
    assert "X-Request-ID" in r.headers


def test_request_id_generated_and_propagated(client):
    # Auto-generated request ID
    r = client.get("/health")
    assert r.status_code == 200
    req_id = r.headers.get("X-Request-ID")
    assert req_id is not None
    assert len(req_id) >= 16

    # Client-supplied request ID propagated back
    custom_id = "test-client-req-999"
    r2 = client.get("/health", headers={"X-Request-ID": custom_id})
    assert r2.status_code == 200
    assert r2.headers.get("X-Request-ID") == custom_id
    assert r2.json()["request_id"] == custom_id


def test_consistent_json_error_format_http_exception(client):
    custom_id = "req-err-404"
    r = client.get("/menu/items/99999", headers={"X-Request-ID": custom_id})
    assert r.status_code == 404
    assert r.headers.get("X-Request-ID") == custom_id
    data = r.json()
    # Backward compatible "detail"
    assert data["detail"] == "Item not found"
    # Consistent structured "error"
    assert "error" in data
    assert data["error"]["code"] == 404
    assert data["error"]["message"] == "Item not found"
    assert data["error"]["request_id"] == custom_id


def test_consistent_json_error_format_validation_error(client):
    custom_id = "req-err-422"
    # POST to /auth/login with missing password
    r = client.post("/auth/login", json={"email": "not-an-email"}, headers={"X-Request-ID": custom_id})
    assert r.status_code == 422
    assert r.headers.get("X-Request-ID") == custom_id
    data = r.json()
    assert "detail" in data
    assert "error" in data
    assert data["error"]["code"] == 422
    assert data["error"]["message"] == "Validation error"
    assert data["error"]["request_id"] == custom_id


def test_trusted_proxy_logic(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,10.0.0.0/8,::1")

    # Trusted IPs
    assert is_trusted_proxy("127.0.0.1") is True
    assert is_trusted_proxy("::1") is True
    assert is_trusted_proxy("10.1.2.3") is True

    # Untrusted IPs
    assert is_trusted_proxy("203.0.113.195") is False
    assert is_trusted_proxy("192.168.1.1") is False
    assert is_trusted_proxy("unknown") is False
    assert is_trusted_proxy("") is False


def test_get_client_ip_untrusted_direct_connection(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,::1")

    # Direct client connection from public IP attempting to spoof X-Forwarded-For
    scope = {
        "type": "http",
        "client": ("203.0.113.50", 54321),
        "headers": [(b"x-forwarded-for", b"198.51.100.77")],
    }
    req = Request(scope)
    # Must NOT trust the header; must return the real connection IP
    assert get_client_ip(req) == "203.0.113.50"


def test_get_client_ip_trusted_reverse_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,::1")

    # Request coming through local reverse proxy (Nginx at 127.0.0.1)
    scope = {
        "type": "http",
        "client": ("127.0.0.1", 45678),
        "headers": [(b"x-forwarded-for", b"203.0.113.50, 10.0.0.1")],
    }
    req = Request(scope)
    # Must trust header and extract the true client IP (first in the chain)
    assert get_client_ip(req) == "203.0.113.50"


def test_get_client_ip_no_forwarded_header():
    scope = {
        "type": "http",
        "client": ("198.51.100.99", 54321),
        "headers": [],
    }
    req = Request(scope)
    assert get_client_ip(req) == "198.51.100.99"
