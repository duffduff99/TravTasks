"""Shared pieces for the route files: templates, database access, login checks, flash messages."""

import hashlib
import hmac
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import clock, queries, rules
from .db import connect, get_setting

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["dayname"] = lambda d: f"{d:%a}"
templates.env.filters["daylong"] = lambda d: f"{d:%A}, {d:%b} {d.day}"
templates.env.filters["dayshort"] = lambda d: f"{d:%a} {d:%b} {d.day}"
templates.env.filters["asdate"] = lambda text: date.fromisoformat(text)
templates.env.globals["SIZE_LABELS"] = rules.SIZE_LABELS


class Redirect(Exception):
    """Raise to send the browser somewhere else (handled in main.py)."""

    def __init__(self, url: str):
        self.url = url


def get_db():
    """One database connection per request. Commits when the request succeeds."""
    db = connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    except Exception:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------- login checks

def require_login(request: Request, db=Depends(get_db)) -> None:
    if not get_setting(db, "password_hash"):
        raise Redirect("/setup")
    if not request.session.get("auth"):
        raise Redirect("/login")


def require_planner(request: Request, db=Depends(get_db), _=Depends(require_login)) -> None:
    """Rule 3: nothing but capture works until every overdue item is decided."""
    if rules.overdue_items(db, clock.today(db)):
        raise Redirect("/review")


def require_token(request: Request, db=Depends(get_db)) -> None:
    """For /api routes: 'Authorization: Bearer <token>' must match the token in Settings."""
    from fastapi import HTTPException

    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, get_setting(db, "api_token")):
        raise HTTPException(status_code=401, detail="Missing or wrong bearer token")


# ---------------------------------------------------------------- flash + rendering

def flash(request: Request, message: str, level: str = "info") -> None:
    """Queue a one-time message for the next page. level: 'info' or 'error'."""
    request.session.setdefault("flash", []).append([level, message])


def render(request: Request, db, template: str, nav: str = "", **context):
    today = clock.today(db)
    context.update(
        nav=nav,
        today=today,
        flashes=request.session.pop("flash", []),
        here=request.url.path + ("?" + request.url.query if request.url.query else ""),
    )
    context.setdefault("counts", queries.counts(db))
    context.setdefault("caps", queries.caps(db))
    return templates.TemplateResponse(request, template, context)


def safe_path(target: str | None) -> str | None:
    """Only allow redirects to pages of this app."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def go_back(request: Request, next_url: str | None = None, default: str = "/"):
    """Redirect to `next_url`, else the page the form was on, else `default`."""
    target = safe_path(next_url)
    if not target:
        referer = urlsplit(request.headers.get("referer", ""))
        if referer.path:
            target = safe_path(referer.path + ("?" + referer.query if referer.query else ""))
    return RedirectResponse(target or default, status_code=303)


def attempt(request: Request, db, action, *args, success: str = "", **kwargs) -> bool:
    """Run a rules.py change. If a rule refuses, show its message instead of failing."""
    try:
        action(db, *args, **kwargs)
    except rules.RuleError as refusal:
        db.rollback()
        flash(request, str(refusal), "error")
        return False
    db.commit()
    if success:
        flash(request, success)
    return True


def parse_day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text or "")
    except ValueError:
        return None
