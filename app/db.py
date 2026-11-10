"""Database connection, schema setup and the settings table.

Plain sqlite3, no ORM. Every request opens its own connection (see web.get_db).
"""

import os
import secrets
import sqlite3
from pathlib import Path

SCHEMA_V1 = (Path(__file__).parent / "schema.sql").read_text()

# To change the schema: append one SQL string here. Never edit an old entry.
# The database remembers how many entries it has applied (PRAGMA user_version).
MIGRATIONS = [
    SCHEMA_V1,
]

# Defaults for everything editable on the Settings page.
DEFAULT_SETTINGS = {
    "task_cap": "3",              # max tasks per day (a quick block counts as one)
    "quick_cap": "5",             # max items in a day's quick lane
    "quick_block_size": "8",      # max quicks inside one quick block
    "max_active_projects": "2",   # projects with a planned next step
    "rollover_hour": "4",         # a "day" starts at this hour
    "timezone": "America/Chicago",
    "iframe_origin": "",          # e.g. https://ha.example.com
}

DEFAULT_KINDS = ["Protect house", "Home upgrade", "Fun build", "Day to day", "Admin"]


def db_path() -> str:
    return os.environ.get("DB_PATH", "data/travtasks.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init(db: sqlite3.Connection) -> None:
    """Create or upgrade the tables and seed first-run data. Safe to call often."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS, start=1):
        if number > version:
            db.executescript(sql)
            db.execute(f"PRAGMA user_version = {number}")
            if number == 1:
                for position, name in enumerate(DEFAULT_KINDS):
                    db.execute("INSERT INTO kinds (name, position) VALUES (?, ?)", (name, position))
    for key, value in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    # Secrets generated once and kept in the database.
    for key in ("session_secret", "api_token"):
        db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, secrets.token_urlsafe(32)),
        )
    db.commit()


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(db: sqlite3.Connection, key: str) -> int:
    return int(get_setting(db, key, DEFAULT_SETTINGS.get(key, "0")))


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
