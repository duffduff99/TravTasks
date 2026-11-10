"""The FastAPI app: startup, login, the JSON API, and the iframe header.

Planner pages live in views.py, the Settings page in settings_views.py.
Run with:  uvicorn app.main:app
"""

import os
import re
import time

from fastapi import Body, Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import clock, queries, rules, settings_views, views
from .db import connect, get_setting, init, set_setting
from .web import (BASE_DIR, Redirect, check_password, flash, get_db, hash_password,
                  render, require_token)

# Create or upgrade the database, and read the cookie-signing secret from it.
_db = connect()
init(_db)
_session_secret = get_setting(_db, "session_secret")
_db.close()

app = FastAPI(title="TravTasks", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(views.planner)
app.include_router(views.always)
app.include_router(settings_views.router)


@app.exception_handler(Redirect)
def handle_redirect(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.middleware("http")
async def allow_iframe(request: Request, call_next):
    """Let the configured origin (Home Assistant) embed the app in an iframe."""
    response = await call_next(request)
    db = connect()
    try:
        origin = get_setting(db, "iframe_origin")
    finally:
        db.close()
    allowed = "'self'" + (" " + origin if re.fullmatch(r"https?://[\w.*:-]+", origin) else "")
    response.headers["Content-Security-Policy"] = f"frame-ancestors {allowed}"
    return response


# Added last so it wraps everything above: request.session works everywhere.
# Set COOKIE_SECURE=true when the app is only reached over HTTPS.
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="travtasks",
    max_age=60 * 60 * 24 * 90,
    same_site="lax",
    https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
)


# ---------------------------------------------------------------- small public files

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


# ---------------------------------------------------------------- login

@app.get("/setup")
def setup_page(request: Request, db=Depends(get_db)):
    if get_setting(db, "password_hash"):
        return RedirectResponse("/login", status_code=303)
    return render(request, db, "login.html", mode="setup", bare=True)


@app.post("/setup")
def setup_save(request: Request, db=Depends(get_db), password: str = Form(""), confirm: str = Form("")):
    if get_setting(db, "password_hash"):
        return RedirectResponse("/login", status_code=303)
    if len(password) < 8 or password != confirm:
        flash(request, "Use at least 8 characters, typed the same twice.", "error")
        return RedirectResponse("/setup", status_code=303)
    set_setting(db, "password_hash", hash_password(password))
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/login")
def login_page(request: Request, db=Depends(get_db)):
    if not get_setting(db, "password_hash"):
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return render(request, db, "login.html", mode="login", bare=True)


@app.post("/login")
def login_check(request: Request, db=Depends(get_db), password: str = Form("")):
    if check_password(password, get_setting(db, "password_hash")):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    time.sleep(1)   # slow down guessing
    flash(request, "Wrong password.", "error")
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- JSON API (bearer token)

@app.post("/api/capture", dependencies=[Depends(require_token)])
def api_capture(db=Depends(get_db), payload: dict = Body(...)):
    """Body: {"title": "...", "notes": "..."} (notes optional). Adds to the inbox."""
    try:
        item_id = rules.capture(db, str(payload.get("title") or ""), str(payload.get("notes") or ""))
    except rules.RuleError as refusal:
        return JSONResponse({"error": str(refusal)}, status_code=400)
    return {"id": item_id, "title": str(payload["title"]).strip(), "status": "inbox"}


@app.get("/api/today", dependencies=[Depends(require_token)])
def api_today(db=Depends(get_db)):
    """Counts and titles for today, for a Home Assistant REST sensor."""
    return queries.today_summary(db, clock.today(db))
