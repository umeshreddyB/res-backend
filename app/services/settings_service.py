from datetime import datetime, time, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.timeutils import LOCAL_TZ, now_utc, to_utc
from ..models import OperatingHour, RestaurantSetting
from ..schemas import (OperatingHourItem, RestaurantRules, SettingsHoursOut,
                       UpdateSettingsHoursIn)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULT_OPEN = time(11, 0)
DEFAULT_CLOSE = time(23, 0)


def get_or_create_settings(db: Session) -> RestaurantSetting:
    setting = db.scalar(select(RestaurantSetting).where(RestaurantSetting.id == 1))
    if not setting:
        setting = RestaurantSetting(
            id=1,
            reservation_duration_minutes=90,
            last_reservation_buffer_minutes=60,
            max_party_size=20,
            max_days_ahead=30,
            advance_percent=50,
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


def get_operating_hours(db: Session) -> list[OperatingHour]:
    hours = list(db.scalars(select(OperatingHour).order_by(OperatingHour.day_of_week)).all())
    if len(hours) < 7:
        existing_days = {h.day_of_week for h in hours}
        for d in range(7):
            if d not in existing_days:
                h = OperatingHour(
                    day_of_week=d,
                    is_closed=False,
                    open_time=DEFAULT_OPEN,
                    close_time=DEFAULT_CLOSE,
                )
                db.add(h)
        db.commit()
        hours = list(db.scalars(select(OperatingHour).order_by(OperatingHour.day_of_week)).all())
    return hours


def get_settings_and_hours(db: Session) -> SettingsHoursOut:
    setting = get_or_create_settings(db)
    hours = get_operating_hours(db)
    rules = RestaurantRules(
        reservation_duration_minutes=setting.reservation_duration_minutes,
        last_reservation_buffer_minutes=setting.last_reservation_buffer_minutes,
        max_party_size=setting.max_party_size,
        max_days_ahead=setting.max_days_ahead,
        advance_percent=setting.advance_percent,
    )
    items = [
        OperatingHourItem(
            day_of_week=h.day_of_week,
            day_name=DAY_NAMES[h.day_of_week],
            is_closed=h.is_closed,
            open_time=h.open_time,
            close_time=h.close_time,
        )
        for h in hours
    ]
    return SettingsHoursOut(rules=rules, hours=items)


def update_settings_and_hours(db: Session, data: UpdateSettingsHoursIn) -> SettingsHoursOut:
    setting = get_or_create_settings(db)
    if data.rules:
        r = data.rules
        setting.reservation_duration_minutes = r.reservation_duration_minutes
        setting.last_reservation_buffer_minutes = r.last_reservation_buffer_minutes
        setting.max_party_size = r.max_party_size
        setting.max_days_ahead = r.max_days_ahead
        if r.advance_percent is not None:
            setting.advance_percent = r.advance_percent

    if data.hours:
        hours_map = {h.day_of_week: h for h in get_operating_hours(db)}
        for item in data.hours:
            if not item.is_closed and (item.open_time is None or item.close_time is None):
                raise HTTPException(400, f"open_time and close_time are required for day {item.day_of_week} when not closed")
            h = hours_map.get(item.day_of_week)
            if h:
                h.is_closed = item.is_closed
                h.open_time = item.open_time
                h.close_time = item.close_time
            else:
                h = OperatingHour(
                    day_of_week=item.day_of_week,
                    is_closed=item.is_closed,
                    open_time=item.open_time,
                    close_time=item.close_time,
                )
                db.add(h)

    db.commit()
    return get_settings_and_hours(db)


def validate_reservation_slot(
    db: Session,
    start_time_raw: datetime,
    guests: int,
) -> tuple[datetime, datetime]:
    """Validates reservation slot against rules, party size, days ahead, and operating hours.

    Handles:
    - Party size bounds
    - Future check & max days ahead
    - Closed days
    - Overnight hours (past midnight e.g. 18:00 to 01:00)
    - Last reservation buffer before closing

    Returns:
        (start_time_utc, end_time_utc)
    """
    settings = get_or_create_settings(db)

    # 1. Party size
    if guests < 1:
        raise HTTPException(400, "Party size must be at least 1")
    if guests > settings.max_party_size:
        raise HTTPException(400, f"Party size {guests} exceeds maximum of {settings.max_party_size}")

    # 2. Timing
    start_utc = to_utc(start_time_raw)
    current_utc = now_utc()
    if start_utc <= current_utc:
        raise HTTPException(400, "Reservation time must be in the future")

    duration = settings.reservation_duration_minutes
    end_utc = start_utc + timedelta(minutes=duration)

    # Convert to restaurant local time for calendar & opening hours checking
    res_start_local = start_utc.astimezone(LOCAL_TZ)
    res_end_local = end_utc.astimezone(LOCAL_TZ)
    now_local = current_utc.astimezone(LOCAL_TZ)

    max_allowed_date = (now_local + timedelta(days=settings.max_days_ahead)).date()
    if res_start_local.date() > max_allowed_date:
        raise HTTPException(400, f"Reservations can only be made up to {settings.max_days_ahead} days in advance")

    # 3. Operating hours checking
    hours = get_operating_hours(db)
    hours_by_day = {h.day_of_week: h for h in hours}

    # A slot can belong to today's shift or yesterday's shift if it ran past midnight
    candidate_dates = [
        res_start_local.date() - timedelta(days=1),
        res_start_local.date(),
    ]

    slot_valid = False
    for shift_date in candidate_dates:
        h = hours_by_day.get(shift_date.weekday())
        if not h or h.is_closed or not h.open_time or not h.close_time:
            continue

        shift_open = datetime.combine(shift_date, h.open_time, tzinfo=LOCAL_TZ)
        if h.close_time > h.open_time:
            # Same day shift, e.g. 11:00 to 23:00
            shift_close = datetime.combine(shift_date, h.close_time, tzinfo=LOCAL_TZ)
        else:
            # Past midnight shift, e.g. 18:00 to 01:00 next day
            shift_close = datetime.combine(shift_date + timedelta(days=1), h.close_time, tzinfo=LOCAL_TZ)

        buffer = settings.last_reservation_buffer_minutes
        # Latest allowed start time must honor both the buffer AND the full dining duration
        shift_latest_start = min(
            shift_close - timedelta(minutes=buffer),
            shift_close - timedelta(minutes=duration),
        )

        if shift_open <= res_start_local <= shift_latest_start and res_end_local <= shift_close:
            slot_valid = True
            break

    if not slot_valid:
        h_today = hours_by_day.get(res_start_local.date().weekday())
        if h_today and h_today.is_closed:
            day_str = DAY_NAMES[res_start_local.date().weekday()]
            raise HTTPException(400, f"Restaurant is closed on {day_str}s")
        raise HTTPException(400, "Reservation time is outside restaurant operating hours")

    return start_utc, end_utc
