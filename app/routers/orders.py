from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core.config import settings
from ..core.timeutils import day_range_utc
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import (MenuItem, Order, OrderItem, OrderStatus, OrderType, Payment,
                      PaymentOption, PaymentStatus, Refund, RefundStatus, Reservation, Role, User)
from ..schemas import (OrderCreated, OrderIn, OrderOut, OrderStatusIn, OrderStatusResult,
                       PaymentInit, RefundIn, RefundOut)
from ..services import petpooja, razorpay_service
from ..services.razorpay_service import to_paise

router = APIRouter(prefix="/orders", tags=["orders"])
admin_router = APIRouter(prefix="/admin/orders", tags=["admin: orders"], dependencies=[Depends(require_admin)])

# allowed status moves for the kitchen/admin
TRANSITIONS = {
    OrderStatus.pending_payment: {OrderStatus.cancelled},
    OrderStatus.placed: {OrderStatus.preparing, OrderStatus.cancelled},
    OrderStatus.preparing: {OrderStatus.ready, OrderStatus.cancelled},
    OrderStatus.ready: {OrderStatus.completed},
    OrderStatus.completed: set(),
    OrderStatus.cancelled: set(),
}


@router.post("", response_model=OrderCreated, status_code=201)
def create_order(data: OrderIn, background: BackgroundTasks,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # merge duplicate lines
    qty: dict[int, int] = {}
    for line in data.items:
        qty[line.menu_item_id] = qty.get(line.menu_item_id, 0) + line.quantity

    menu = {m.id: m for m in db.scalars(select(MenuItem).where(MenuItem.id.in_(qty.keys())))}
    missing = [i for i in qty if i not in menu]
    if missing:
        raise HTTPException(400, f"Unknown menu item id(s): {missing}")
    sold_out = [m.name for m in menu.values() if not m.is_available]
    if sold_out:
        raise HTTPException(409, f"Currently unavailable: {', '.join(sold_out)}")

    if data.reservation_id:
        res = db.get(Reservation, data.reservation_id)
        if not res or res.user_id != user.id:
            raise HTTPException(404, "Reservation not found")

    # price is always calculated on the server from the database, never trusted from the client
    total = sum((menu[i].price * q for i, q in qty.items()), Decimal("0"))

    if data.payment_option == PaymentOption.full_online:
        pay_now = total
    elif data.payment_option == PaymentOption.advance_online:
        pay_now = (total * settings.advance_percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    else:
        pay_now = Decimal("0")

    order = Order(
        user_id=user.id, order_type=data.order_type, payment_option=data.payment_option,
        status=OrderStatus.pending_payment if pay_now > 0 else OrderStatus.placed,
        payment_status=PaymentStatus.unpaid, total=total, amount_paid=Decimal("0"),
        customer_name=data.customer_name or user.name, customer_phone=data.customer_phone or user.phone,
        notes=data.notes, pickup_time=data.pickup_time, reservation_id=data.reservation_id,
        items=[OrderItem(menu_item_id=i, name=menu[i].name, unit_price=menu[i].price, quantity=q)
               for i, q in qty.items()],
    )
    db.add(order)
    db.flush()  # get order.id

    payment_init: Optional[PaymentInit] = None
    if pay_now > 0:
        try:
            rz = razorpay_service.create_order(pay_now, receipt=f"order_{order.id}")
        except httpx.HTTPError:
            db.rollback()
            raise HTTPException(502, "Payment gateway unavailable. Please try again.")
        db.add(Payment(order_id=order.id, razorpay_order_id=rz["id"], amount=pay_now))
        payment_init = PaymentInit(razorpay_order_id=rz["id"], key_id=settings.razorpay_key_id,
                                   amount=pay_now, amount_paise=rz["amount"], currency=rz.get("currency", "INR"))
    db.commit()

    if order.status == OrderStatus.placed:  # pay-at-pickup: confirmed immediately
        background.add_task(petpooja.push_order, order.id)

    out = OrderCreated.model_validate(order)
    out.payment = payment_init
    return out


@router.get("/me", response_model=list[OrderOut])
def my_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db),
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    q = (select(Order).options(selectinload(Order.items)).where(Order.user_id == user.id)
         .order_by(Order.id.desc()).limit(limit).offset(offset))
    return db.scalars(q).all()


@router.get("/{order_id}", response_model=OrderOut)
def get_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == order_id))
    if not order or (order.user_id != user.id and user.role != Role.admin):
        raise HTTPException(404, "Order not found")
    return order


# ---------------- admin ----------------
@admin_router.get("", response_model=list[OrderOut])
def list_orders(status: Optional[OrderStatus] = None, order_type: Optional[OrderType] = None,
                on_date: Optional[date] = Query(None, alias="date", description="Local date, YYYY-MM-DD"),
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                db: Session = Depends(get_db)):
    """Admin order board. Poll this every few seconds in V1 (V2 adds WebSocket push)."""
    q = select(Order).options(selectinload(Order.items))
    if status:
        q = q.where(Order.status == status)
    if order_type:
        q = q.where(Order.order_type == order_type)
    if on_date:
        lo, hi = day_range_utc(on_date)
        q = q.where(Order.created_at >= lo, Order.created_at < hi)
    return db.scalars(q.order_by(Order.id.desc()).limit(limit).offset(offset)).all()


@admin_router.patch("/{order_id}/status", response_model=OrderStatusResult)
def set_order_status(order_id: int, data: OrderStatusIn, db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == order_id))
    if not order:
        raise HTTPException(404, "Order not found")
    if data.status not in TRANSITIONS[order.status]:
        raise HTTPException(409, f"Cannot move order from {order.status.value} to {data.status.value}")
    order.status = data.status
    # When cancelling a paid/partially-paid order, flag for the admin to consider a refund
    refund_rec = False
    if data.status == OrderStatus.cancelled and order.amount_paid > order.amount_refunded:
        refund_rec = True
    db.commit()
    out = OrderStatusResult.model_validate(order)
    out.refund_recommended = refund_rec
    return out


@admin_router.post("/{order_id}/mark-paid", response_model=OrderOut)
def mark_paid(order_id: int, db: Session = Depends(get_db)):
    """Staff collected the remaining amount at the counter (pay-at-pickup, or the balance of a 50% advance)."""
    order = db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == order_id))
    if not order:
        raise HTTPException(404, "Order not found")
    if order.status == OrderStatus.cancelled:
        raise HTTPException(409, "Order is cancelled")
    order.amount_paid = order.total
    order.payment_status = PaymentStatus.paid
    db.commit()
    return order


@admin_router.post("/{order_id}/refund", response_model=RefundOut, status_code=201)
def refund_order(order_id: int, data: RefundIn = RefundIn(), db: Session = Depends(get_db)):
    """Refund an order (full or partial). Locks the order row to prevent concurrent over-refunding."""
    order = db.scalar(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    if not order:
        raise HTTPException(404, "Order not found")

    refundable = order.amount_paid - order.amount_refunded
    if refundable <= 0:
        raise HTTPException(409, "Nothing to refund")

    amount = data.amount if data.amount is not None else refundable
    if amount > refundable:
        raise HTTPException(400, f"Refund amount {amount} exceeds refundable balance {refundable}")

    # Find a paid payment to refund against
    paid_payments = [p for p in order.payments
                     if p.status.value == "paid" and p.razorpay_payment_id]
    if not paid_payments:
        raise HTTPException(409, "No paid payment record to refund against")
    payment = paid_payments[0]

    try:
        rz = razorpay_service.refund_payment(payment.razorpay_payment_id, to_paise(amount))
    except httpx.HTTPError:
        db.rollback()
        raise HTTPException(502, "Payment gateway unavailable. Please try again.")

    refund = Refund(
        order_id=order.id,
        razorpay_payment_id=payment.razorpay_payment_id,
        razorpay_refund_id=rz.get("id"),
        amount=amount,
        status=RefundStatus.processed if rz.get("status") == "processed" else RefundStatus.created,
    )
    db.add(refund)

    # In mock mode (or instant refunds), update totals immediately
    if refund.status == RefundStatus.processed:
        order.amount_refunded += amount
        if order.amount_refunded >= order.amount_paid:
            order.payment_status = PaymentStatus.refunded
        else:
            order.payment_status = PaymentStatus.partially_refunded

    db.commit()
    return refund
