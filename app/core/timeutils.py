from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings

LOCAL_TZ = ZoneInfo(settings.timezone)


def to_utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as restaurant local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def day_range_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
