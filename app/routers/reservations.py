from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..core.config import settings
from ..core.timeutils import day_range_utc, now_utc, to_utc
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import DiningTable, Reservation, ReservationStatus, User
from ..schemas import ReservationIn, ReservationOut, ReservationStatusIn
from ..services.booking import free_tables, lock_table, table_is_free
from ..services.settings_service import validate_reservation_slot

router = APIRouter(prefix="/reservations", tags=["reservations"])
admin_router = APIRouter(prefix="/admin/reservations", tags=["admin: reservations"],
                         dependencies=[Depends(require_admin)])

_load = (joinedload(Reservation.user), joinedload(Reservation.table))


@router.post("", response_model=ReservationOut, status_code=201)
def create_reservation(data: ReservationIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    start, end = validate_reservation_slot(db, data.start_time, data.guests)

    if data.table_id:
        table = db.get(DiningTable, data.table_id)
        if not table or not table.is_available:
            raise HTTPException(404, "Table not found or not available")
        if table.capacity < data.guests:
            raise HTTPException(400, f"Table {table.number} seats {table.capacity}, party is {data.guests}")
        candidates = [table]
    else:
        candidates = free_tables(db, start, end, data.guests)

    for t in candidates:
        lock_table(db, t.id)
        if table_is_free(db, t.id, start, end):
            res = Reservation(user_id=user.id, table_id=t.id, guests=data.guests,
                              start_time=start, end_time=end, notes=data.notes)
            db.add(res)
            db.commit()
            return db.scalar(select(Reservation).options(*_load).where(Reservation.id == res.id))
        db.rollback()  # release the lock before trying the next candidate
    raise HTTPException(409, "No table is free for that time. Please choose another slot.")


@router.get("/me", response_model=list[ReservationOut])
def my_reservations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = (select(Reservation).options(*_load).where(Reservation.user_id == user.id)
         .order_by(Reservation.start_time.desc()))
    return db.scalars(q).unique().all()


@router.post("/{reservation_id}/cancel", response_model=ReservationOut)
def cancel_reservation(reservation_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    res = db.scalar(select(Reservation).options(*_load).where(Reservation.id == reservation_id))
    if not res or res.user_id != user.id:
        raise HTTPException(404, "Reservation not found")
    if res.status != ReservationStatus.confirmed:
        raise HTTPException(409, f"Reservation is already {res.status.value}")
    res.status = ReservationStatus.cancelled
    db.commit()
    return res


# ---------------- admin ----------------
@admin_router.get("", response_model=list[ReservationOut])
def list_reservations(on_date: Optional[date] = Query(None, alias="date", description="Local date, YYYY-MM-DD"),
                      status: Optional[ReservationStatus] = None,
                      limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                      db: Session = Depends(get_db)):
    q = select(Reservation).options(*_load)
    if on_date:
        lo, hi = day_range_utc(on_date)
        q = q.where(Reservation.start_time >= lo, Reservation.start_time < hi)
    if status:
        q = q.where(Reservation.status == status)
    q = q.order_by(Reservation.start_time).limit(limit).offset(offset)
    return db.scalars(q).unique().all()


@admin_router.patch("/{reservation_id}/status", response_model=ReservationOut)
def set_reservation_status(reservation_id: int, data: ReservationStatusIn, db: Session = Depends(get_db)):
    res = db.scalar(select(Reservation).options(*_load).where(Reservation.id == reservation_id))
    if not res:
        raise HTTPException(404, "Reservation not found")
    res.status = data.status
    db.commit()
    return res
