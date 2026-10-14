from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DiningTable, Reservation, ReservationStatus


def _overlapping(start: datetime, end: datetime):
    return (Reservation.status == ReservationStatus.confirmed,
            Reservation.start_time < end,
            Reservation.end_time > start)


def table_is_free(db: Session, table_id: int, start: datetime, end: datetime) -> bool:
    q = select(Reservation.id).where(Reservation.table_id == table_id, *_overlapping(start, end)).limit(1)
    return db.scalar(q) is None


def free_tables(db: Session, start: datetime, end: datetime, guests: int) -> list[DiningTable]:
    busy = select(Reservation.table_id).where(*_overlapping(start, end))
    q = (select(DiningTable)
         .where(DiningTable.is_available.is_(True), DiningTable.capacity >= guests, DiningTable.id.not_in(busy))
         .order_by(DiningTable.capacity, DiningTable.number))
    return list(db.scalars(q))


def lock_table(db: Session, table_id: int) -> Optional[DiningTable]:
    """Row lock (Postgres) so two people cannot book the same table for the same slot at the same moment."""
    return db.scalar(select(DiningTable).where(DiningTable.id == table_id).with_for_update())
