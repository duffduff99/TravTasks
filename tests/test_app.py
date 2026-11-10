"""Tests for the web layer: login, the API, the review lock, calendars, and the Jira import."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest
from fastapi.testclient import TestClient

from app import calendars, clock, rules
from app.db import connect, get_setting, set_setting
from scripts.import_jira import import_rows

PASSWORD = "correct horse"
SECRET_URL = "https://calendar.example.com/private-token-abc123/basic.ics"


@pytest.fixture
def app_db():
    """The file database the web app uses, emptied before each test."""
    from app import main  # noqa: F401  (creates the tables)
    db = connect()
    for table in ("items", "quick_blocks", "calendar_feeds"):
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM settings WHERE key IN ('password_hash', 'reopened_day', 'iframe_origin')")
    db.commit()
    yield db
    db.close()


@pytest.fixture
def client(app_db):
    from app.main import app
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def logged_in(client):
    client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD})
    return client


def token(db):
    return {"Authorization": "Bearer " + get_setting(db, "api_token")}


# ---------------------------------------------------------------- login

def test_first_run_goes_to_setup_then_requires_login(client):
    assert client.get("/").headers["location"] == "/setup"
    client.post("/setup", data={"password": PASSWORD, "confirm": PASSWORD})
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/").headers["location"] == "/login"
    assert client.post("/setup", data={"password": "another one", "confirm": "another one"}).headers["location"] == "/login"


def test_wrong_password_is_refused(logged_in, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    logged_in.post("/logout")
    logged_in.post("/login", data={"password": "nope"})
    assert logged_in.get("/").headers["location"] == "/login"
    logged_in.post("/login", data={"password": PASSWORD})
    assert logged_in.get("/").status_code == 200


@pytest.mark.parametrize("path", ["/", "/today", "/week", "/backlog", "/backlog?status=someday",
                                  "/done", "/more", "/settings"])
def test_pages_render(logged_in, app_db, path):
    rules.capture(app_db, "Something")
    app_db.commit()
    assert logged_in.get(path).status_code == 200


# ---------------------------------------------------------------- API

def test_api_needs_the_bearer_token(client):
    assert client.post("/api/capture", json={"title": "x"}).status_code == 401
    assert client.get("/api/today", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_api_capture_adds_to_inbox(client, app_db):
    response = client.post("/api/capture", json={"title": "From my phone"}, headers=token(app_db))
    assert response.status_code == 200
    item = rules.get_item(app_db, response.json()["id"])
    assert (item["title"], item["status"]) == ("From my phone", "inbox")
    assert client.post("/api/capture", json={"title": " "}, headers=token(app_db)).status_code == 400


def test_api_today_reports_counts_and_titles(client, app_db):
    today = clock.today(app_db)
    task = rules.capture(app_db, "Caulk tub")
    quick = rules.capture(app_db, "Pay bill")
    app_db.execute("UPDATE items SET size = 'task' WHERE id = ?", (task,))
    app_db.execute("UPDATE items SET size = 'quick' WHERE id = ?", (quick,))
    rules.plan_item(app_db, task, today, today)
    rules.plan_item(app_db, quick, today, today)
    rules.complete_item(app_db, quick)
    rules.capture(app_db, "Unsorted")
    app_db.commit()
    data = client.get("/api/today", headers=token(app_db)).json()
    assert data["open_count"] == 1 and data["done_count"] == 1
    assert data["open_tasks"] == ["Caulk tub"] and data["open_quicks"] == []
    assert data["inbox_count"] == 1 and data["done_for_today"] is False


# ---------------------------------------------------------------- rules through the web

def leftover_from_yesterday(db):
    yesterday = clock.today(db) - timedelta(days=1)
    item = rules.capture(db, "Leftover")
    db.execute("UPDATE items SET size = 'task', status = 'planned', planned_date = ? WHERE id = ?",
               (yesterday.isoformat(), item))
    db.commit()
    return item


def test_review_blocks_the_planner_but_not_capture(logged_in, app_db):
    item = leftover_from_yesterday(app_db)
    for path in ("/", "/today", "/week", "/backlog", "/settings"):
        assert logged_in.get(path).headers["location"] == "/review"
    assert "Leftover" in logged_in.get("/review").text
    logged_in.post("/capture", data={"title": "Still works"})
    assert app_db.execute("SELECT COUNT(*) FROM items WHERE title = 'Still works'").fetchone()[0] == 1
    logged_in.post(f"/review/{item}", data={"action": "backlog"})
    assert logged_in.get("/").status_code == 200


def test_done_screen_replaces_today_and_hides_the_backlog(logged_in, app_db):
    today = clock.today(app_db)
    task = rules.capture(app_db, "Only task")
    app_db.execute("UPDATE items SET size = 'task' WHERE id = ?", (task,))
    rules.plan_item(app_db, task, today, today)
    rules.capture(app_db, "Tempting backlog item")
    app_db.commit()
    assert "Done for today" not in logged_in.get("/today").text
    logged_in.post(f"/items/{task}/toggle")
    page = logged_in.get("/today").text
    assert "Done for today" in page and "Tempting backlog item" not in page
    logged_in.post("/today/reopen")
    assert "Done for today" not in logged_in.get("/today").text


def test_drag_over_the_cap_is_refused_with_a_message(logged_in, app_db):
    today = clock.today(app_db)
    ids = []
    for n in range(4):
        item = rules.capture(app_db, f"Task {n}")
        app_db.execute("UPDATE items SET size = 'task', status = 'backlog' WHERE id = ?", (item,))
        ids.append(item)
    app_db.commit()
    for item in ids:
        logged_in.post("/move", json={"id": item, "target": f"day:{today}", "ids": [item]})
    assert rules.day_load(app_db, today)["tasks"] == 3
    assert rules.get_item(app_db, ids[3])["status"] == "backlog"
    assert "Remove one first" in logged_in.get("/week").text


# ---------------------------------------------------------------- iframe + calendar secrets

def test_iframe_header_allows_only_the_configured_origin(logged_in, app_db):
    assert logged_in.get("/more").headers["content-security-policy"] == "frame-ancestors 'self'"
    set_setting(app_db, "iframe_origin", "https://ha.example.com")
    app_db.commit()
    assert logged_in.get("/more").headers["content-security-policy"] == \
        "frame-ancestors 'self' https://ha.example.com"


def test_calendar_url_never_reaches_the_browser(logged_in, app_db):
    logged_in.post("/settings/feeds", data={"label": "Family", "url": SECRET_URL, "mode": "full"})
    assert app_db.execute("SELECT url FROM calendar_feeds").fetchone()["url"] == SECRET_URL
    page = logged_in.get("/settings").text
    assert "Family" in page and "private-token" not in page


def test_calendar_fetch_failure_does_not_log_the_url(app_db, monkeypatch, caplog):
    def explode(url, **kwargs):
        raise RuntimeError(f"could not reach {url}")
    monkeypatch.setattr(calendars.httpx, "get", explode)
    app_db.execute("INSERT INTO calendar_feeds (label, url, mode) VALUES ('Family', ?, 'full')", (SECRET_URL,))
    today = date(2026, 11, 10)
    with caplog.at_level("DEBUG"):
        assert calendars.events_by_day(app_db, today, today, ZoneInfo("America/Chicago")) == {}
    assert "Family" in caplog.text and "private-token" not in caplog.text


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:1
DTSTART:20261110T150000Z
DTEND:20261110T160000Z
SUMMARY:Dentist
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20261110
DTEND;VALUE=DATE:20261111
SUMMARY:Trash day
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART:20261103T230000Z
DTEND:20261104T000000Z
RRULE:FREQ=WEEKLY
SUMMARY:Class
END:VEVENT
END:VCALENDAR
"""


def test_calendar_modes():
    tz = ZoneInfo("America/Chicago")
    day = date(2026, 11, 10)
    calendar = icalendar.Calendar.from_ical(ICS)
    full = calendars.events_from_calendar(calendar, "full", day, day, tz)[day]
    assert {e["title"] for e in full} == {"Dentist", "Trash day", "Class"}   # weekly repeat included
    assert next(e for e in full if e["title"] == "Dentist")["when"] == "9:00 AM – 10:00 AM"
    busy = calendars.events_from_calendar(calendar, "busy_only", day, day, tz)[day]
    assert [e["title"] for e in busy] == ["Busy", "Busy"]                    # all-day event skipped


# ---------------------------------------------------------------- Jira import

def test_jira_import_mapping(db):
    rows = [
        {"title": "Swap bulb", "key": "HOME-1", "type": "Quick", "status": "To Do", "category": "To Do", "notes": ""},
        {"title": "Build sauna", "key": "HOME-2", "type": "Task", "status": "Freezer", "category": "To Do", "notes": "maybe"},
        {"title": "Fix fence", "key": "HOME-3", "type": "Task", "status": "In Progress", "category": "In Progress", "notes": ""},
        {"title": "Old thing", "key": "HOME-4", "type": "Task", "status": "Done", "category": "Done", "notes": ""},
    ]
    result = import_rows(db, rows)
    assert (result["inbox"], result["someday"], result["skipped_done"]) == (2, 1, 1)
    found = {r["source_ref"]: r for r in db.execute("SELECT * FROM items")}
    assert (found["HOME-1"]["size"], found["HOME-1"]["status"]) == ("quick", "inbox")
    assert (found["HOME-2"]["size"], found["HOME-2"]["status"]) == (None, "someday")
    assert found["HOME-3"]["status"] == "inbox"
    assert import_rows(db, rows)["skipped_existing"] == 3    # running it twice adds nothing
