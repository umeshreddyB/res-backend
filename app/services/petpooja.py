"""PetPooja POS integration service.

Orders confirmed with status 'placed' are pushed to PetPooja so billing and kitchen
order tickets (KOT) happen on the restaurant's existing POS.

CRITICAL:
Per client instructions, DO NOT guess the exact PetPooja save_order payload.
This module provides the clean client structure, separated mapping function,
retry loop, callback stubs, and unit test harness ready for the exact PetPooja
fields once the official documentation and sandbox credentials are provided.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.config import settings
from ..db import SessionLocal
from ..models import Order, OrderStatus

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def map_order_to_petpooja_payload(order: Order) -> dict:
    """Transforms an internal Order into the PetPooja order payload.

    NOTE: The exact schema structure below is a clean mapping draft.
    Update the field keys to match the official PetPooja API documentation
    once provided by the client.
    """
    items_payload = []
    for item in order.items:
        items_payload.append({
            "item_id": str(item.menu_item_id or ""),
            "name": item.name,
            "price": float(item.unit_price),
            "quantity": item.quantity,
            "total": float(item.line_total),
        })

    payload = {
        "restaurant_id": settings.petpooja_rest_id,
        "order_id": str(order.id),
        "order_type": order.order_type.value,
        "customer": {
            "name": order.customer_name,
            "phone": order.customer_phone or "",
        },
        "payment": {
            "option": order.payment_option.value,
            "status": order.payment_status.value,
            "amount_paid": float(order.amount_paid or 0),
            "total": float(order.total),
        },
        "order_items": items_payload,
        "notes": order.notes or "",
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }
    return payload


def push_order(order_id: int, http_client: Optional[httpx.Client] = None) -> bool:
    """Pushes a single confirmed order to PetPooja.

    Tracks status, attempt counts, and error messages on the Order record.
    Returns True if successfully pushed, False otherwise.
    """
    if not settings.petpooja_enabled:
        log.info("PetPooja disabled in configuration; skipping push for order %s", order_id)
        return False

    with SessionLocal() as db:
        order = db.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
        )
        if not order:
            log.warning("Order %s not found for PetPooja push", order_id)
            return False

        order.petpooja_attempts = (order.petpooja_attempts or 0) + 1
        payload = map_order_to_petpooja_payload(order)

        headers = {
            "Content-Type": "application/json",
            "X-App-Key": settings.petpooja_app_key,
            "X-App-Secret": settings.petpooja_app_secret,
            "Authorization": f"Bearer {settings.petpooja_access_token}" if settings.petpooja_access_token else "",
        }

        url = f"{settings.petpooja_base_url.rstrip('/')}/orders/save"

        try:
            if http_client:
                resp = http_client.post(url, json=payload, headers=headers, timeout=10.0)
            else:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(url, json=payload, headers=headers)

            resp.raise_for_status()
            data = resp.json() if resp.content else {}

            # PetPooja returned order reference
            petpooja_ref = data.get("petpooja_order_id") or data.get("order_id") or f"pp_{order.id}"
            order.petpooja_order_id = str(petpooja_ref)
            order.petpooja_status = "success"
            order.petpooja_pushed_at = utcnow()
            order.petpooja_last_error = None
            db.commit()
            log.info("PetPooja push successful for order %s (ref: %s)", order_id, petpooja_ref)
            return True

        except Exception as e:
            err_msg = str(e)
            order.petpooja_status = "failed"
            order.petpooja_last_error = err_msg
            db.commit()
            log.error("PetPooja push failed for order %s (attempt %s): %s",
                      order_id, order.petpooja_attempts, err_msg)
            return False


def retry_failed_pushes(max_attempts: int = 5, http_client: Optional[httpx.Client] = None) -> int:
    """Finds orders that failed to push to PetPooja and retries them.

    Returns the count of successfully pushed orders.
    """
    if not settings.petpooja_enabled:
        return 0

    with SessionLocal() as db:
        q = (
            select(Order.id)
            .where(
                Order.status == OrderStatus.placed,
                Order.petpooja_status == "failed",
                Order.petpooja_attempts < max_attempts,
            )
            .order_by(Order.id)
        )
        failed_ids = list(db.scalars(q).all())

    success_count = 0
    for order_id in failed_ids:
        if push_order(order_id, http_client=http_client):
            success_count += 1
    return success_count


def handle_order_status_callback(db: Session, payload: dict) -> dict:
    """Processes incoming status updates from PetPooja POS (e.g. accepted, preparing, ready)."""
    petpooja_order_id = payload.get("order_id") or payload.get("petpooja_order_id")
    new_status = payload.get("status")
    if not petpooja_order_id:
        return {"status": "error", "message": "Missing order_id in callback"}

    order = db.scalar(select(Order).where(Order.petpooja_order_id == str(petpooja_order_id)))
    if not order:
        return {"status": "not_found", "message": f"Order {petpooja_order_id} not found"}

    # Map POS status strings to OrderStatus if applicable
    log.info("PetPooja status callback for order %s: %s", order.id, new_status)
    return {"status": "ok", "order_id": order.id, "petpooja_status": new_status}


def handle_menu_sync(db: Session, payload: dict) -> dict:
    """Processes incoming menu synchronization from PetPooja POS."""
    log.info("PetPooja menu sync received with %s items/categories", len(payload.get("items", [])))
    return {"status": "ok", "message": "Menu sync stub ready"}
