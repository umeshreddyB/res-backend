from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import OrderStatus, Payment, PaymentRecordStatus, PaymentStatus


def apply_payment_success(payment: Payment, razorpay_payment_id: str) -> bool:
    """Mark a payment as paid and update its order. Idempotent: returns True only the first time.

    Call this with the payment row locked (SELECT ... FOR UPDATE) so that the frontend verify call
    and the Razorpay webhook cannot both apply the same payment.

    If the order has been cancelled (e.g. by the abandoned-order cleanup), we still record the
    payment so the money is not lost, and flag the order for admin attention.
    """
    if payment.status == PaymentRecordStatus.paid:
        return False
    payment.status = PaymentRecordStatus.paid
    payment.razorpay_payment_id = razorpay_payment_id
    order = payment.order
    order.amount_paid = (order.amount_paid or Decimal("0")) + payment.amount
    order.payment_status = (PaymentStatus.paid if order.amount_paid >= order.total
                            else PaymentStatus.partially_paid)
    if order.status == OrderStatus.pending_payment:
        order.status = OrderStatus.placed
    elif order.status == OrderStatus.cancelled:
        # Late payment for a cancelled order — keep the money recorded, flag for admin review
        order.needs_attention = True
    return True
