from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from ..models import (OrderStatus, Payment, PaymentRecordStatus, PaymentStatus,
                      Reservation, ReservationStatus)


def apply_payment_success(payment: Payment, razorpay_payment_id: str, db: Optional[Session] = None) -> bool:
    """Mark a payment as paid and update its order. Idempotent: returns True only the first time.

    Call this with the payment row locked (SELECT ... FOR UPDATE) so that the frontend verify call
    and the Razorpay webhook cannot both apply the same payment.

    If the order has been cancelled (e.g. by the abandoned-order cleanup), we still record the
    payment so the money is not lost, and flag the order for admin attention.
    For dine-in orders:
    - If pending_payment: reservation becomes confirmed in the same transaction.
    - If cancelled (late payment): if the table is still free, re-confirm it. If not,
      keep cancelled, mark paid, set needs_attention, and do not auto-assign a table.
    """
    if payment.status == PaymentRecordStatus.paid:
        return False
    payment.status = PaymentRecordStatus.paid
    payment.razorpay_payment_id = razorpay_payment_id
    order = payment.order
    order.amount_paid = (order.amount_paid or Decimal("0")) + payment.amount
    order.payment_status = (PaymentStatus.paid if order.amount_paid >= order.total
                            else PaymentStatus.partially_paid)

    res: Optional[Reservation] = None
    if order.reservation_id:
        if db is not None:
            res = db.get(Reservation, order.reservation_id)
        else:
            res = order.reservation

    if order.status == OrderStatus.pending_payment:
        order.status = OrderStatus.placed
        if res and res.status == ReservationStatus.held:
            res.status = ReservationStatus.confirmed
            res.hold_expires_at = None
    elif order.status == OrderStatus.cancelled:
        order.needs_attention = True
        if res and res.status == ReservationStatus.cancelled and db is not None:
            from .booking import lock_table, table_is_free
            lock_table(db, res.table_id)
            if table_is_free(db, res.table_id, res.start_time, res.end_time):
                res.status = ReservationStatus.confirmed
                res.hold_expires_at = None
                order.status = OrderStatus.placed
                order.needs_attention = False
    return True
