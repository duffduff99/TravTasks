"""What "today" means. A day rolls over at 4:00 AM local time, not midnight."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import get_int, get_setting


def get_tz(db) -> ZoneInfo:
    try:
        return ZoneInfo(get_setting(db, "timezone", "America/Chicago"))
    except Exception:
        return ZoneInfo("America/Chicago")


def planner_day(moment: datetime, tz: ZoneInfo, rollover_hour: int) -> date:
    """The planner day a moment belongs to. 2:00 AM Wednesday is still Tuesday."""
    return (moment.astimezone(tz) - timedelta(hours=rollover_hour)).date()


def today(db, now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return planner_day(now, get_tz(db), get_int(db, "rollover_hour"))


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
