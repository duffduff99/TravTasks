import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

# Tests use a throwaway database file. Set before anything imports the app.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import rules  # noqa: E402
from app.db import connect, init  # noqa: E402

TODAY = date(2026, 11, 10)   # a Tuesday


@pytest.fixture
def db():
    """A fresh, empty in-memory database for each test."""
    connection = connect(":memory:")
    init(connection)
    yield connection
    connection.close()


@pytest.fixture
def make(db):
    """make("Title", size="task", status="backlog", parent_id=None) -> item id"""
    def _make(title="Item", size="task", status="backlog", parent_id=None):
        item_id = rules.capture(db, title)
        db.execute(
            "UPDATE items SET size = ?, status = ?, parent_id = ? WHERE id = ?",
            (size, status, parent_id, item_id),
        )
        return item_id
    return _make
