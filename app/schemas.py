from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

from .models import (OrderStatus, OrderType, PaymentOption, PaymentStatus,
                     RefundStatus, ReservationStatus, Role)

# money is returned to the frontend as a JSON number (e.g. 250.0), not a string
Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------
class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=64)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(ORM):
    id: int
    name: str
    email: str
    phone: Optional[str] = None
    role: Role


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ---------- menu ----------
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    sort_order: Optional[int] = None


class ItemIn(BaseModel):
    category_id: int
    name: str = Field(min_length=1, max_length=150)
    description: Optional[str] = None
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    image_url: Optional[str] = None
    is_veg: bool = True
    is_available: bool = True


class ItemUpdate(BaseModel):
    category_id: Optional[int] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    image_url: Optional[str] = None
    is_veg: Optional[bool] = None
    is_available: Optional[bool] = None


class AvailabilityIn(BaseModel):
    is_available: bool


class ItemOut(ORM):
    id: int
    category_id: int
    name: str
    description: Optional[str]
    price: Money
    image_url: Optional[str]
    is_veg: bool
    is_available: bool


class CategoryOut(ORM):
    id: int
    name: str
    sort_order: int


class CategoryWithItems(CategoryOut):
    items: list[ItemOut]


# ---------- tables ----------
class TableIn(BaseModel):
    number: int = Field(ge=1)
    capacity: int = Field(ge=1, le=50)
    is_available: bool = True


class TableUpdate(BaseModel):
    number: Optional[int] = Field(default=None, ge=1)
    capacity: Optional[int] = Field(default=None, ge=1, le=50)
    is_available: Optional[bool] = None


class TableOut(ORM):
    id: int
    number: int
    capacity: int
    is_available: bool


# ---------- reservations ----------
class ReservationIn(BaseModel):
    guests: int = Field(ge=1, le=50)
    start_time: datetime            # ISO 8601, ideally with offset e.g. 2026-10-20T19:30:00+05:30
    table_id: Optional[int] = None  # omit to auto-assign the smallest free table that fits
    notes: Optional[str] = None


class ReservationOut(ORM):
    id: int
    table_id: int
    guests: int
    start_time: datetime
    end_time: datetime
    status: ReservationStatus
    notes: Optional[str]
    created_at: datetime
    user: Optional[UserOut] = None
    table: Optional[TableOut] = None


class ReservationStatusIn(BaseModel):
    status: ReservationStatus


# ---------- orders ----------
class OrderItemIn(BaseModel):
    menu_item_id: int
    quantity: int = Field(ge=1, le=50)


class OrderIn(BaseModel):
    order_type: OrderType = OrderType.takeaway
    items: list[OrderItemIn] = Field(min_length=1)
    payment_option: PaymentOption = PaymentOption.full_online
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    notes: Optional[str] = None
    pickup_time: Optional[datetime] = None
    reservation_id: Optional[int] = None


class OrderItemOut(ORM):
    menu_item_id: Optional[int]
    name: str
    unit_price: Money
    quantity: int
    line_total: Money


class OrderOut(ORM):
    id: int
    order_type: OrderType
    status: OrderStatus
    payment_option: PaymentOption
    payment_status: PaymentStatus
    total: Money
    amount_paid: Money
    amount_refunded: Money
    balance: Money
    customer_name: str
    customer_phone: Optional[str]
    notes: Optional[str]
    pickup_time: Optional[datetime]
    reservation_id: Optional[int]
    needs_attention: bool = False
    created_at: datetime
    items: list[OrderItemOut]


class PaymentInit(BaseModel):
    """Everything the frontend needs to open Razorpay Checkout."""
    razorpay_order_id: str
    key_id: str
    amount: Money
    amount_paise: int
    currency: str = "INR"


class OrderCreated(OrderOut):
    payment: Optional[PaymentInit] = None


class OrderStatusIn(BaseModel):
    status: OrderStatus


class VerifyPaymentIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class OrderStatusResult(OrderOut):
    """Returned when admin changes order status. Includes refund_recommended flag."""
    refund_recommended: bool = False


class RefundIn(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0, max_digits=10, decimal_places=2,
                                       description="Partial refund amount. Omit for full refund.")


class RefundOut(ORM):
    id: int
    order_id: int
    razorpay_payment_id: str
    razorpay_refund_id: Optional[str]
    amount: Money
    status: RefundStatus
    created_at: datetime
