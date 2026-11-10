"""The Settings page: limits, kinds, calendar feeds, API token, password."""

import re
import secrets
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request

from . import calendars, queries, rules
from .db import get_setting, set_setting
from .web import (attempt, check_password, flash, get_db, go_back, hash_password, render,
                  require_planner)

router = APIRouter(prefix="/settings", dependencies=[Depends(require_planner)])

# Number settings and the range each one accepts.
NUMBER_SETTINGS = {
    "task_cap": (1, 10),
    "quick_cap": (0, 20),
    "quick_block_size": (1, 30),
    "max_active_projects": (1, 10),
    "rollover_hour": (0, 12),
}


@router.get("")
def settings_page(request: Request, db=Depends(get_db)):
    # Note: feed URLs are deliberately NOT selected here. They must never reach the browser.
    feeds = db.execute("SELECT id, label, mode FROM calendar_feeds ORDER BY id").fetchall()
    values = {key: get_setting(db, key) for key in
              (*NUMBER_SETTINGS, "timezone", "iframe_origin", "api_token")}
    return render(request, db, "settings.html", nav="more", values=values, feeds=feeds,
                  kinds=queries.kinds(db), base_url=str(request.base_url).rstrip("/"))


@router.post("/general")
def save_general(request: Request, db=Depends(get_db),
                 task_cap: int = Form(...), quick_cap: int = Form(...),
                 quick_block_size: int = Form(...), max_active_projects: int = Form(...),
                 rollover_hour: int = Form(...), timezone: str = Form(...),
                 iframe_origin: str = Form("")):
    numbers = {"task_cap": task_cap, "quick_cap": quick_cap,
               "quick_block_size": quick_block_size,
               "max_active_projects": max_active_projects, "rollover_hour": rollover_hour}
    for key, value in numbers.items():
        low, high = NUMBER_SETTINGS[key]
        if not low <= value <= high:
            flash(request, f"{key.replace('_', ' ')} must be between {low} and {high}.", "error")
            return go_back(request)
    try:
        ZoneInfo(timezone.strip())
    except Exception:
        flash(request, "Unknown timezone. Use a name like America/Chicago.", "error")
        return go_back(request)
    iframe_origin = iframe_origin.strip().rstrip("/")
    if iframe_origin and not re.fullmatch(r"https?://[\w.*:-]+", iframe_origin):
        flash(request, "The iframe origin should look like https://ha.example.com", "error")
        return go_back(request)
    for key, value in numbers.items():
        set_setting(db, key, value)
    set_setting(db, "timezone", timezone.strip())
    set_setting(db, "iframe_origin", iframe_origin)
    flash(request, "Settings saved.")
    return go_back(request)


@router.post("/password")
def save_password(request: Request, db=Depends(get_db), current: str = Form(""),
                  password: str = Form(""), confirm: str = Form("")):
    if not check_password(current, get_setting(db, "password_hash")):
        flash(request, "The current password is wrong.", "error")
    elif len(password) < 8 or password != confirm:
        flash(request, "Use at least 8 characters, typed the same twice.", "error")
    else:
        set_setting(db, "password_hash", hash_password(password))
        flash(request, "Password changed.")
    return go_back(request)


@router.post("/token")
def new_token(request: Request, db=Depends(get_db)):
    set_setting(db, "api_token", secrets.token_urlsafe(32))
    flash(request, "New API token created. Update your shortcuts and Home Assistant.")
    return go_back(request)


# ---------------------------------------------------------------- kinds

@router.post("/kinds")
def kind_add(request: Request, db=Depends(get_db), name: str = Form("")):
    attempt(request, db, rules.add_kind, name)
    return go_back(request)


@router.post("/kinds/{kind_id}/rename")
def kind_rename(request: Request, kind_id: int, db=Depends(get_db), name: str = Form("")):
    attempt(request, db, rules.rename_kind, kind_id, name, success="Kind renamed.")
    return go_back(request)


@router.post("/kinds/{kind_id}/delete")
def kind_delete(request: Request, kind_id: int, db=Depends(get_db), move_to: int = Form(0)):
    attempt(request, db, rules.delete_kind, kind_id, move_to or None, success="Kind deleted.")
    return go_back(request)


# ---------------------------------------------------------------- calendar feeds

@router.post("/feeds")
def feed_add(request: Request, db=Depends(get_db), label: str = Form(""),
             url: str = Form(""), mode: str = Form("busy_only")):
    label, url = label.strip(), url.strip()
    if not label or not re.match(r"(https?|webcal)://", url) or mode not in ("full", "busy_only"):
        flash(request, "A feed needs a name and a link starting with https:// or webcal://", "error")
        return go_back(request)
    db.execute("INSERT INTO calendar_feeds (label, url, mode) VALUES (?, ?, ?)", (label, url, mode))
    flash(request, f"Calendar '{label}' added.")
    return go_back(request)


@router.post("/feeds/{feed_id}/mode")
def feed_mode(request: Request, feed_id: int, db=Depends(get_db), mode: str = Form(...)):
    if mode in ("full", "busy_only"):
        db.execute("UPDATE calendar_feeds SET mode = ? WHERE id = ?", (mode, feed_id))
    return go_back(request)


@router.post("/feeds/{feed_id}/delete")
def feed_delete(request: Request, feed_id: int, db=Depends(get_db)):
    db.execute("DELETE FROM calendar_feeds WHERE id = ?", (feed_id,))
    calendars.forget(feed_id)
    flash(request, "Calendar removed.")
    return go_back(request)
