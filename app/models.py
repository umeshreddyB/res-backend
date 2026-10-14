import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (Boolean, DateTime, Enum, ForeignKey, Integer, Numeric,
                        String, Text, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def E(enum_cls):
    # stored as plain strings: no Postgres ENUM types to migrate when values are added (e.g. "delivery" in V2)
    return Enum(enum_cls, native_enum=False, length=30, values_callable=lambda x: [e.value for e in x])


class Role(str, enum.Enum):
    customer = "customer"
    admin = "admin"


class OrderType(str, enum.Enum):
    takeaway = "takeaway"
    dine_in = "dine_in"
    # V2: delivery


class OrderStatus(str, enum.Enum):
    pending_payment = "pending_payment"
    placed = "placed"
    preparing = "preparing"
    ready = "ready"
    completed = "completed"
    cancelled = "cancelled"


class PaymentStatus(str, enum.Enum):
    unpaid = "unpaid"
    partially_paid = "partially_paid"
    paid = "paid"
    partially_refunded = "partially_refunded"
    refunded = "refunded"


class PaymentOption(str, enum.Enum):
    full_online = "full_online"
    advance_online = "advance_online"
    pay_at_pickup = "pay_at_pickup"


class PaymentRecordStatus(str, enum.Enum):
    created = "created"
    paid = "paid"
    failed = "failed"


class RefundStatus(str, enum.Enum):
    created = "created"
    processed = "processed"
    failed = "failed"


class ReservationStatus(str, enum.Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(E(Role), default=Role.customer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Category(Base):
    __tablename__ = "menu_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list["MenuItem"]] = relationship(back_populates="category", cascade="all, delete-orphan",
                                                    order_by="MenuItem.name")


class MenuItem(Base):
    __tablename__ = "menu_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("menu_categories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    image_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_veg: Mapped[bool] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    petpooja_item_id: Mapped[Optional[str]] = mapped_column(String(100))  # for PetPooja menu sync
    category: Mapped[Category] = relationship(back_populates="items")


class DiningTable(Base):
    __tablename__ = "dining_tables"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    capacity: Mapped[int] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)  # admin switch (out of service etc.)


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("dining_tables.id"), index=True)
    guests: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReservationStatus] = mapped_column(E(ReservationStatus), default=ReservationStatus.confirmed)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship()
    table: Mapped[DiningTable] = relationship()
    __table_args__ = (Index("ix_res_table_time", "table_id", "start_time", "end_time"),)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    order_type: Mapped[OrderType] = mapped_column(E(OrderType))
    status: Mapped[OrderStatus] = mapped_column(E(OrderStatus), default=OrderStatus.pending_payment, index=True)
    payment_option: Mapped[PaymentOption] = mapped_column(E(PaymentOption))
    payment_status: Mapped[PaymentStatus] = mapped_column(E(PaymentStatus), default=PaymentStatus.unpaid)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    amount_refunded: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    customer_name: Mapped[str] = mapped_column(String(100))
    customer_phone: Mapped[Optional[str]] = mapped_column(String(20))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    pickup_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reservation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("reservations.id"))
    petpooja_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="order")
    user: Mapped[User] = relationship()

    @property
    def balance(self) -> Decimal:
        return self.total - (self.amount_paid or Decimal("0"))


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("menu_items.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(150))          # snapshot, so later menu edits don't change old orders
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    quantity: Mapped[int] = mapped_column(Integer)
    order: Mapped[Order] = relationship(back_populates="items")

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    razorpay_order_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[PaymentRecordStatus] = mapped_column(E(PaymentRecordStatus), default=PaymentRecordStatus.created)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    order: Mapped[Order] = relationship(back_populates="payments")


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    razorpay_payment_id: Mapped[str] = mapped_column(String(100))  # the payment being refunded
    razorpay_refund_id: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[RefundStatus] = mapped_column(E(RefundStatus), default=RefundStatus.created)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    order: Mapped[Order] = relationship(back_populates="refunds")
