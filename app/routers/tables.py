from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.timeutils import to_utc
from ..db import get_db
from ..deps import require_admin
from ..models import DiningTable, Reservation, ReservationStatus
from ..schemas import TableIn, TableOut, TableUpdate
from ..services.booking import free_tables
from ..services.settings_service import validate_reservation_slot

router = APIRouter(prefix="/tables", tags=["tables"])
admin_router = APIRouter(prefix="/admin/tables", tags=["admin: tables"], dependencies=[Depends(require_admin)])


@router.get("/availability", response_model=list[TableOut])
def table_availability(start_time: datetime = Query(..., description="Wanted slot start, ISO 8601"),
                       guests: int = Query(1, ge=1, le=50),
                       db: Session = Depends(get_db)):
    """Tables that are free for the slot (start_time to start_time + reservation length) and fit the party."""
    start, end = validate_reservation_slot(db, start_time, guests)
    return free_tables(db, start, end, guests)


# ---------------- admin ----------------
@admin_router.get("", response_model=list[TableOut])
def list_tables(db: Session = Depends(get_db)):
    return db.scalars(select(DiningTable).order_by(DiningTable.number)).all()


@admin_router.post("", response_model=TableOut, status_code=201)
def create_table(data: TableIn, db: Session = Depends(get_db)):
    if db.scalar(select(DiningTable).where(DiningTable.number == data.number)):
        raise HTTPException(409, "Table number already exists")
    table = DiningTable(**data.model_dump())
    db.add(table)
    db.commit()
    return table


@admin_router.patch("/{table_id}", response_model=TableOut)
def update_table(table_id: int, data: TableUpdate, db: Session = Depends(get_db)):
    table = db.get(DiningTable, table_id)
    if not table:
        raise HTTPException(404, "Table not found")
    changes = data.model_dump(exclude_unset=True)
    if "number" in changes and changes["number"] != table.number and \
            db.scalar(select(DiningTable).where(DiningTable.number == changes["number"])):
        raise HTTPException(409, "Table number already exists")
    for k, v in changes.items():
        setattr(table, k, v)
    db.commit()
    return table


@admin_router.delete("/{table_id}", status_code=204)
def delete_table(table_id: int, db: Session = Depends(get_db)):
    table = db.get(DiningTable, table_id)
    if not table:
        raise HTTPException(404, "Table not found")
    has_res = db.scalar(select(Reservation.id).where(Reservation.table_id == table_id).limit(1))
    if has_res:
        raise HTTPException(409, "Table has reservations; mark it unavailable instead of deleting")
    db.delete(table)
    db.commit()
