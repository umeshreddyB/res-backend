"""Cleanup job for orders stuck in pending_payment.

Multi-worker safe: uses a single UPDATE with a WHERE guard so two workers running
at the same time cannot cancel the same order twice or conflict.
"""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import update

from ..core.config import settings
from ..core.timeutils import now_utc
from ..db import SessionLocal
from ..models import Order, OrderStatus

log = logging.getLogger(__name__)


def cancel_abandoned_orders(timeout_minutes: int | None = None) -> int:
    """Cancel orders stuck in pending_payment for longer than timeout_minutes.

    Returns the number of orders cancelled. Safe for concurrent invocation by
    multiple workers — the UPDATE WHERE guard ensures each order is only touched once.
    """
    timeout = timeout_minutes or settings.abandoned_order_timeout_minutes
    cutoff = now_utc() - timedelta(minutes=timeout)
    with SessionLocal() as db:
        result = db.execute(
            update(Order)
            .where(
                Order.status == OrderStatus.pending_payment,
                Order.created_at < cutoff,
            )
            .values(status=OrderStatus.cancelled)
        )
        db.commit()
        count = result.rowcount
        if count:
            log.info("Cancelled %d abandoned order(s) older than %d minutes", count, timeout)
        return count


async def abandoned_order_loop(interval_seconds: int = 60):
    """Run cancel_abandoned_orders periodically. Started in the app lifespan."""
    while True:
        try:
            cancel_abandoned_orders()
        except Exception:
            log.exception("Error in abandoned order cleanup")
        await asyncio.sleep(interval_seconds)
