from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..schemas import SettingsHoursOut, UpdateSettingsHoursIn
from ..services.settings_service import (get_settings_and_hours,
                                         update_settings_and_hours)

router = APIRouter(prefix="/settings", tags=["settings"])
admin_router = APIRouter(prefix="/admin/settings", tags=["admin: settings"],
                         dependencies=[Depends(require_admin)])


@router.get("/hours", response_model=SettingsHoursOut)
def get_public_hours(db: Session = Depends(get_db)):
    """Public: get restaurant opening hours and reservation rules."""
    return get_settings_and_hours(db)


@admin_router.get("/hours", response_model=SettingsHoursOut)
def get_admin_hours(db: Session = Depends(get_db)):
    """Admin: get restaurant opening hours and reservation rules."""
    return get_settings_and_hours(db)


@admin_router.put("/hours", response_model=SettingsHoursOut)
def update_hours(data: UpdateSettingsHoursIn, db: Session = Depends(get_db)):
    """Admin: update restaurant opening hours (including closed days & past-midnight hours) and rules."""
    return update_settings_and_hours(db, data)
