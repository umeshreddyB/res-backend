"""Cleanup job for orders stuck in pending_payment.

Multi-worker safe: uses a single UPDATE with a WHERE guard so two workers running
at the same time cannot cancel the same order twice or conflict.
"""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_, select, update

from ..core.config import settings
from ..core.timeutils import now_utc
from ..db import SessionLocal
from ..models import Order, OrderStatus, Reservation, ReservationStatus

log = logging.getLogger(__name__)


def cancel_abandoned_orders(timeout_minutes: int | None = None) -> int:
    """Cancel orders stuck in pending_payment for longer than timeout_minutes, or whose

    held reservations have expired. Also cancels the held reservation releasing the table hold.
    Safe for concurrent invocation by multiple workers (SELECT ... FOR UPDATE SKIP LOCKED).
    Returns the number of orders cancelled.
    """
    timeout = timeout_minutes or settings.abandoned_order_timeout_minutes
    now = now_utc()
    cutoff = now - timedelta(minutes=timeout)
    with SessionLocal() as db:
        # Orders with expired held reservations
        expired_res_order_ids = (
            select(Order.id)
            .join(Reservation, Order.reservation_id == Reservation.id)
            .where(
                Order.status == OrderStatus.pending_payment,
                Reservation.status == ReservationStatus.held,
                Reservation.hold_expires_at < now,
            )
        )

        candidate_ids_stmt = (
            select(Order.id)
            .where(
                Order.status == OrderStatus.pending_payment,
                or_(
                    Order.created_at < cutoff,
                    Order.id.in_(expired_res_order_ids),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        order_ids = list(db.scalars(candidate_ids_stmt).all())

        if not order_ids:
            # Also cancel any orphaned expired held reservations
            db.execute(
                update(Reservation)
                .where(
                    Reservation.status == ReservationStatus.held,
                    Reservation.hold_expires_at < now,
                )
                .values(status=ReservationStatus.cancelled)
            )
            db.commit()
            return 0

        # Fetch locked orders
        orders = list(db.scalars(
            select(Order).where(Order.id.in_(order_ids)).with_for_update()
        ).all())

        res_ids_to_cancel = [o.reservation_id for o in orders if o.reservation_id]

        # Guarded cancel on orders
        db.execute(
            update(Order)
            .where(
                Order.id.in_([o.id for o in orders]),
                Order.status == OrderStatus.pending_payment,
            )
            .values(status=OrderStatus.cancelled)
        )

        # Cancel held reservations associated with cancelled orders
        if res_ids_to_cancel:
            db.execute(
                update(Reservation)
                .where(
                    Reservation.id.in_(res_ids_to_cancel),
                    Reservation.status == ReservationStatus.held,
                )
                .values(status=ReservationStatus.cancelled)
            )

        # Cancel any other expired held reservations
        db.execute(
            update(Reservation)
            .where(
                Reservation.status == ReservationStatus.held,
                Reservation.hold_expires_at < now,
            )
            .values(status=ReservationStatus.cancelled)
        )

        db.commit()
        count = len(orders)
        if count:
            log.info("Cancelled %d abandoned order(s)", count)
        return count


async def abandoned_order_loop(interval_seconds: int = 60):
    """Run cancel_abandoned_orders periodically. Started in the app lifespan."""
    while True:
        try:
            cancel_abandoned_orders()
        except Exception:
            log.exception("Error in abandoned order cleanup")
        await asyncio.sleep(interval_seconds)
