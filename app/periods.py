"""Calendar periods for training history, using the owner's local time zone."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.timeutil import to_iso

PERIODS = (("today", "Today"), ("week", "Week"), ("month", "Month"), ("all", "All time"))


def history_period(value: str, *, now: datetime | None = None, timezone_name: str = "America/Toronto") -> dict:
    key = value if value in dict(PERIODS) else "all"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end = None
    title = "All-time totals"
    if key == "today":
        start, end = midnight, midnight + timedelta(days=1)
        title = "Today's totals"
    elif key == "week":
        start = midnight - timedelta(days=midnight.weekday())
        end = start + timedelta(days=7)
        title = "This week's totals"
    elif key == "month":
        start = midnight.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        title = "This month's totals"
    if start is None:
        caption = "All saved sessions · including sessions without a start date"
    else:
        last = end - timedelta(days=1)
        caption = start.strftime("%b %d, %Y") if key == "today" else f"{start:%b %d} – {last:%b %d, %Y}"
        caption += f" · {zone.key}"
    return {
        "key": key, "index": [p[0] for p in PERIODS].index(key),
        "choices": PERIODS, "title": title, "caption": caption,
        "start": to_iso(start) if start else None, "end": to_iso(end) if end else None,
        "record_label": "Personal record" if key == "all" else "Best working set in this period",
    }
