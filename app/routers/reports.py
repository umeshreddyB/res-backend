import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..core.config import settings
from ..core.timeutils import LOCAL_TZ
from ..db import get_db
from ..deps import require_admin
from ..models import (Order, OrderItem, OrderStatus, OrderType, PaymentStatus,
                      Reservation)
from ..schemas import (DailyReportItem, OrderTypeSummary, SummaryReportOut,
                      TopItemReportItem)

admin_router = APIRouter(
    prefix="/admin/reports",
    tags=["admin: reports"],
    dependencies=[Depends(require_admin)],
)


def parse_date_range(from_str: Optional[str], to_str: Optional[str]) -> tuple[date, date, datetime, datetime]:
    today = datetime.now(LOCAL_TZ).date()
    if from_str:
        try:
            from_date = date.fromisoformat(from_str)
        except ValueError:
            raise HTTPException(400, "Invalid 'from' date format. Use YYYY-MM-DD")
    else:
        from_date = today

    if to_str:
        try:
            to_date = date.fromisoformat(to_str)
        except ValueError:
            raise HTTPException(400, "Invalid 'to' date format. Use YYYY-MM-DD")
    else:
        to_date = today

    if from_date > to_date:
        raise HTTPException(400, "'from' date cannot be after 'to' date")

    if (to_date - from_date).days > 366:
        raise HTTPException(400, "Date range cannot exceed 366 days")

    start_utc = datetime.combine(from_date, time.min, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end_utc = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ).astimezone(timezone.utc)

    return from_date, to_date, start_utc, end_utc


def sanitize_csv_field(val: Any) -> str:
    """Prevent CSV formula injection by prefixing '=', '+', '-', '@' with a single quote."""
    if val is None:
        return ""
    s = str(val)
    if s and s[0] in ("=", "+", "-", "@"):
        return f"'{s}"
    return s


# SQL expressions for net sales and pending balance
net_sales_expr = case(
    (
        or_(
            Order.status != OrderStatus.cancelled,
            and_(Order.amount_paid.is_not(None), Order.amount_paid > 0),
        ),
        func.coalesce(Order.amount_paid, 0) - func.coalesce(Order.amount_refunded, 0),
    ),
    else_=0,
)

pending_amount_expr = case(
    (
        and_(
            Order.status.in_([
                OrderStatus.pending_payment,
                OrderStatus.placed,
                OrderStatus.preparing,
                OrderStatus.ready,
            ]),
            Order.total > func.coalesce(Order.amount_paid, 0),
        ),
        Order.total - func.coalesce(Order.amount_paid, 0),
    ),
    else_=0,
)


@admin_router.get("/summary", response_model=SummaryReportOut)
def get_summary_report(
    from_date: Optional[str] = Query(None, alias="from", description="Local start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="Local end date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
):
    """Sales summary report for the admin dashboard within the specified date range."""
    _, _, start_utc, end_utc = parse_date_range(from_date, to_date)

    stmt = select(
        func.coalesce(func.sum(net_sales_expr), 0).label("net_sales"),
        func.coalesce(func.sum(func.coalesce(Order.amount_paid, 0)), 0).label("gross_paid"),
        func.coalesce(func.sum(func.coalesce(Order.amount_refunded, 0)), 0).label("total_refunded"),
        func.coalesce(func.sum(case((Order.status != OrderStatus.cancelled, 1), else_=0)), 0).label("orders_count"),
        func.coalesce(func.sum(case((Order.status == OrderStatus.cancelled, 1), else_=0)), 0).label("cancelled_count"),
        func.coalesce(func.sum(pending_amount_expr), 0).label("pending_amount"),
    ).where(Order.created_at >= start_utc, Order.created_at < end_utc)

    row = db.execute(stmt).one()
    net_sales = Decimal(str(row.net_sales))
    gross_paid = Decimal(str(row.gross_paid))
    total_refunded = Decimal(str(row.total_refunded))
    orders_count = int(row.orders_count)
    cancelled_count = int(row.cancelled_count)
    pending_amount = Decimal(str(row.pending_amount))

    average_order_value = (net_sales / orders_count).quantize(Decimal("0.01")) if orders_count > 0 else Decimal("0.00")

    # By order type
    by_type_stmt = (
        select(
            Order.order_type,
            func.coalesce(func.sum(case((Order.status != OrderStatus.cancelled, 1), else_=0)), 0).label("orders"),
            func.coalesce(func.sum(net_sales_expr), 0).label("net_sales"),
        )
        .where(Order.created_at >= start_utc, Order.created_at < end_utc)
        .group_by(Order.order_type)
    )
    by_order_type = [
        OrderTypeSummary(
            order_type=r.order_type,
            orders=int(r.orders),
            net_sales=Decimal(str(r.net_sales)),
        )
        for r in db.execute(by_type_stmt)
    ]

    # By payment status
    by_payment_stmt = (
        select(
            Order.payment_status,
            func.count(Order.id).label("count"),
        )
        .where(Order.created_at >= start_utc, Order.created_at < end_utc)
        .group_by(Order.payment_status)
    )
    by_payment_status = {ps.value: 0 for ps in PaymentStatus}
    for r in db.execute(by_payment_stmt):
        status_key = r.payment_status.value if hasattr(r.payment_status, "value") else str(r.payment_status)
        by_payment_status[status_key] = int(r.count)

    return SummaryReportOut(
        net_sales=net_sales,
        gross_paid=gross_paid,
        total_refunded=total_refunded,
        orders_count=orders_count,
        cancelled_count=cancelled_count,
        average_order_value=average_order_value,
        by_order_type=by_order_type,
        by_payment_status=by_payment_status,
        pending_amount=pending_amount,
    )


@admin_router.get("/daily", response_model=list[DailyReportItem])
def get_daily_report(
    from_date: Optional[str] = Query(None, alias="from", description="Local start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="Local end date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
):
    """Daily sales aggregation in restaurant timezone for the specified date range."""
    _, _, start_utc, end_utc = parse_date_range(from_date, to_date)

    if db.bind.dialect.name == "postgresql":
        date_col = func.date(func.timezone(settings.timezone, Order.created_at)).label("date")
    else:
        offset = datetime.now(LOCAL_TZ).utcoffset()
        total_minutes = int(offset.total_seconds()) // 60 if offset else 0
        mod = f"{'+' if total_minutes >= 0 else ''}{total_minutes} minutes"
        date_col = func.date(Order.created_at, mod).label("date")

    stmt = (
        select(
            date_col,
            func.coalesce(func.sum(net_sales_expr), 0).label("net_sales"),
            func.coalesce(func.sum(func.coalesce(Order.amount_paid, 0)), 0).label("gross_paid"),
            func.coalesce(func.sum(func.coalesce(Order.amount_refunded, 0)), 0).label("total_refunded"),
            func.coalesce(func.sum(case((Order.status != OrderStatus.cancelled, 1), else_=0)), 0).label("orders_count"),
            func.coalesce(func.sum(case((Order.status == OrderStatus.cancelled, 1), else_=0)), 0).label("cancelled_count"),
        )
        .where(Order.created_at >= start_utc, Order.created_at < end_utc)
        .group_by(date_col)
        .order_by(date_col.asc())
    )

    rows = db.execute(stmt).all()
    return [
        DailyReportItem(
            date=str(r.date),
            net_sales=Decimal(str(r.net_sales)),
            gross_paid=Decimal(str(r.gross_paid)),
            total_refunded=Decimal(str(r.total_refunded)),
            orders_count=int(r.orders_count),
            cancelled_count=int(r.cancelled_count),
        )
        for r in rows
    ]


@admin_router.get("/top-items", response_model=list[TopItemReportItem])
def get_top_items_report(
    from_date: Optional[str] = Query(None, alias="from", description="Local start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="Local end date (YYYY-MM-DD)"),
    limit: int = Query(10, ge=1, le=100, description="Max number of items to return"),
    db: Session = Depends(get_db),
):
    """Top selling items by quantity sold within the specified date range."""
    _, _, start_utc, end_utc = parse_date_range(from_date, to_date)

    stmt = (
        select(
            OrderItem.menu_item_id.label("item_id"),
            OrderItem.name.label("item_name"),
            func.sum(OrderItem.quantity).label("quantity_sold"),
            func.sum(OrderItem.unit_price * OrderItem.quantity).label("net_sales"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.created_at >= start_utc,
            Order.created_at < end_utc,
            Order.status != OrderStatus.cancelled,
        )
        .group_by(OrderItem.menu_item_id, OrderItem.name)
        .order_by(func.sum(OrderItem.quantity).desc(), func.sum(OrderItem.unit_price * OrderItem.quantity).desc())
        .limit(limit)
    )

    rows = db.execute(stmt).all()
    return [
        TopItemReportItem(
            item_id=r.item_id,
            item_name=r.item_name,
            quantity_sold=int(r.quantity_sold),
            net_sales=Decimal(str(r.net_sales)),
        )
        for r in rows
    ]


@admin_router.get("/orders.csv")
def export_orders_csv(
    from_date: Optional[str] = Query(None, alias="from", description="Local start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="Local end date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
):
    """Export orders in CSV format with formula injection protection."""
    from_d, to_d, start_utc, end_utc = parse_date_range(from_date, to_date)

    stmt = (
        select(Order)
        .options(joinedload(Order.reservation).joinedload(Reservation.table))
        .where(Order.created_at >= start_utc, Order.created_at < end_utc)
        .order_by(Order.created_at.asc())
    )
    orders = db.scalars(stmt).all()

    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "order_id",
        "created_at",
        "order_type",
        "table_number",
        "status",
        "payment_status",
        "total_amount",
        "amount_paid",
        "amount_refunded",
        "net_amount",
        "customer_name",
        "customer_phone",
    ]
    writer.writerow(headers)

    for o in orders:
        if o.status == OrderStatus.cancelled and (o.amount_paid is None or o.amount_paid <= 0):
            net_amount = 0.0
        else:
            net_amount = float(o.amount_paid - o.amount_refunded)

        dt = o.created_at
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            created_str = dt.astimezone(LOCAL_TZ).isoformat()
        else:
            created_str = ""

        row = [
            sanitize_csv_field(o.id),
            sanitize_csv_field(created_str),
            sanitize_csv_field(o.order_type.value),
            sanitize_csv_field(o.table_number if o.table_number is not None else ""),
            sanitize_csv_field(o.status.value),
            sanitize_csv_field(o.payment_status.value),
            sanitize_csv_field(float(o.total)),
            sanitize_csv_field(float(o.amount_paid)),
            sanitize_csv_field(float(o.amount_refunded)),
            sanitize_csv_field(net_amount),
            sanitize_csv_field(o.customer_name),
            sanitize_csv_field(o.customer_phone or ""),
        ]
        writer.writerow(row)

    csv_data = output.getvalue()
    filename = f"orders_{from_d.isoformat()}_{to_d.isoformat()}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
