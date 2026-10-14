import hashlib
import hmac
import uuid
from decimal import Decimal

import httpx

from ..core.config import settings


def to_paise(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1")))


def create_order(amount: Decimal, receipt: str) -> dict:
    """Create a Razorpay order. With no RAZORPAY_KEY_ID set (dev), returns a mock order - no network call."""
    paise = to_paise(amount)
    if not settings.razorpay_key_id:
        return {"id": f"order_mock_{uuid.uuid4().hex[:14]}", "amount": paise, "currency": "INR"}
    resp = httpx.post(
        "https://api.razorpay.com/v1/orders",
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        json={"amount": paise, "currency": "INR", "receipt": receipt},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    expected = _hmac(settings.razorpay_key_secret, f"{order_id}|{payment_id}".encode())
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret:
        return False
    return hmac.compare_digest(_hmac(settings.razorpay_webhook_secret, raw_body), signature)


def refund_payment(razorpay_payment_id: str, amount_paise: int) -> dict:
    """Create a Razorpay refund (full or partial).

    In mock mode (no RAZORPAY_KEY_ID), returns a fake refund id.
    """
    if not settings.razorpay_key_id:
        return {"id": f"rfnd_mock_{uuid.uuid4().hex[:14]}", "amount": amount_paise,
                "payment_id": razorpay_payment_id, "status": "processed"}
    resp = httpx.post(
        f"https://api.razorpay.com/v1/payments/{razorpay_payment_id}/refunds",
        auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
        json={"amount": amount_paise},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

