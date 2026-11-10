"""Read-only calendar events from ICS feeds.

The feed URLs are secrets. They are fetched here on the server and are never
logged, never put in an error message and never sent to the browser.
"""

import logging
import time as time_module
from datetime import date, datetime, time, timedelta

import httpx
import icalendar
import recurring_ical_events

log = logging.getLogger("travtasks.calendars")
# httpx logs every request URL at INFO level. Silence it to keep feed URLs out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

CACHE_SECONDS = 15 * 60
_cache: dict = {}   # feed id -> (time fetched, parsed calendar or None)


def forget(feed_id: int) -> None:
    """Drop a feed from the cache (call when it is changed or deleted)."""
    _cache.pop(feed_id, None)


def _load(feed):
    """Return the parsed calendar for a feed, fetching it at most every 15 minutes."""
    hit = _cache.get(feed["id"])
    if hit and time_module.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    calendar = hit[1] if hit else None   # keep showing the old copy if the fetch fails
    try:
        url = feed["url"].strip()
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        response = httpx.get(url, timeout=8, follow_redirects=True)
        response.raise_for_status()
        calendar = icalendar.Calendar.from_ical(response.content)
    except Exception as exc:
        # Only the exception type: its message can contain the secret URL.
        log.warning("Calendar feed '%s' could not be loaded (%s)", feed["label"], type(exc).__name__)
    _cache[feed["id"]] = (time_module.time(), calendar)
    return calendar


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def events_from_calendar(calendar, mode: str, first: date, last: date, tz) -> dict:
    """Events between two days (inclusive) as {day: [event, ...]}.

    mode 'full' shows titles. mode 'busy_only' shows timed events as "Busy"
    and skips all-day events.
    """
    result: dict = {}
    window_start = datetime.combine(first, time.min, tz)
    window_end = datetime.combine(last + timedelta(days=1), time.min, tz)
    for event in recurring_ical_events.of(calendar).between(window_start, window_end):
        start = event["DTSTART"].dt
        end = event["DTEND"].dt if event.get("DTEND") else start
        all_day = not isinstance(start, datetime)
        if all_day and mode == "busy_only":
            continue
        title = "Busy" if mode == "busy_only" else str(event.get("SUMMARY") or "(no title)")
        if all_day:
            first_day, last_day = start, max(start, end - timedelta(days=1))
        else:
            start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
            end = end.replace(tzinfo=tz) if end.tzinfo is None else end.astimezone(tz)
            first_day = start.date()
            last_day = max(first_day, (end - timedelta(seconds=1)).date())
        day = max(first_day, first)
        while day <= min(last_day, last):
            if all_day:
                when, sort_key = "All day", ""
            else:
                begins = _clock(start) if day == first_day else "…"
                finishes = _clock(end) if day == last_day else "…"
                when = f"{begins} – {finishes}"
                sort_key = start.isoformat() if day == first_day else "0"
            result.setdefault(day, []).append({"when": when, "title": title, "sort": sort_key})
            day += timedelta(days=1)
    return result


def events_by_day(db, first: date, last: date, tz) -> dict:
    """All feeds merged: {day: [{'when': ..., 'title': ...}, ...]} sorted by time."""
    merged: dict = {}
    for feed in db.execute("SELECT * FROM calendar_feeds ORDER BY id").fetchall():
        calendar = _load(feed)
        if calendar is None:
            continue
        try:
            found = events_from_calendar(calendar, feed["mode"], first, last, tz)
        except Exception as exc:
            log.warning("Calendar feed '%s' could not be read (%s)", feed["label"], type(exc).__name__)
            continue
        for day, events in found.items():
            merged.setdefault(day, []).extend(events)
    for events in merged.values():
        events.sort(key=lambda e: e["sort"])
    return merged
