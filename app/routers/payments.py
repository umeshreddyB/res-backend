import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..deps import get_current_user
from ..models import Order, OrderStatus, Payment, User
from ..schemas import OrderOut, VerifyPaymentIn
from ..services import petpooja, razorpay_service
from ..services.payments import apply_payment_success

router = APIRouter(prefix="/payments", tags=["payments"])


def _locked_payment(db: Session, razorpay_order_id: str) -> Payment | None:
    return db.scalar(select(Payment).where(Payment.razorpay_order_id == razorpay_order_id).with_for_update())


@router.post("/verify", response_model=OrderOut)
def verify_payment(data: VerifyPaymentIn, background: BackgroundTasks,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Frontend calls this after Razorpay Checkout succeeds, passing the 3 values Razorpay returns."""
    payment = _locked_payment(db, data.razorpay_order_id)
    if not payment or payment.order.user_id != user.id:
        raise HTTPException(404, "Payment not found")
    if not razorpay_service.verify_payment_signature(data.razorpay_order_id, data.razorpay_payment_id,
                                                     data.razorpay_signature):
        raise HTTPException(400, "Invalid payment signature")
    applied = apply_payment_success(payment, data.razorpay_payment_id)
    db.commit()
    order = db.scalar(select(Order).options(selectinload(Order.items)).where(Order.id == payment.order_id))
    if applied and order.status == OrderStatus.placed:
        background.add_task(petpooja.push_order, order.id)
    return order


@router.post("/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request, background: BackgroundTasks,
                           x_razorpay_signature: str = Header(default=""), db: Session = Depends(get_db)):
    """Safety net: confirms payments even if the customer closes the browser before /verify is called.
    Configure in the Razorpay dashboard: URL = https://<your-domain>/payments/webhook, events = payment.captured"""
    raw = await request.body()
    if not razorpay_service.verify_webhook_signature(raw, x_razorpay_signature):
        raise HTTPException(400, "Invalid webhook signature")
    body = json.loads(raw)
    if body.get("event") in ("payment.captured", "order.paid"):
        entity = body.get("payload", {}).get("payment", {}).get("entity", {})
        payment = _locked_payment(db, entity.get("order_id", ""))
        if payment and apply_payment_success(payment, entity.get("id", "")):
            db.commit()
            if payment.order.status == OrderStatus.placed:
                background.add_task(petpooja.push_order, payment.order_id)
        else:
            db.rollback()
    return {"status": "ok"}
