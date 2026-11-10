"""The rules of the planner. Every change to an item goes through this file.

These rules are the product. Each one is covered in tests/test_rules.py.
When a rule says no, the function raises RuleError with a message for the user
and changes nothing.

  1. Daily cap: a day holds at most `task_cap` tasks and `quick_cap` quicks.
     A quick block uses one task slot and holds up to `quick_block_size` quicks.
     Finished items still count. Over the cap is refused, never squeezed in.
  2. Projects and big-ticket items can never be planned, only their next steps.
     At most `max_active_projects` projects may have a planned step at once.
  3. Nothing rolls over by itself. Unfinished items from past days must each
     be decided in the review: today, another day, backlog or drop.
  4. When everything planned today is done, today is closed. Nothing more can
     be planned on it unless it is reopened on purpose.
"""

from datetime import date

from .clock import utc_now_text
from .db import get_int, get_setting, set_setting

SIZES = ("quick", "task", "project", "big_ticket")
SCHEDULABLE = ("quick", "task")          # sizes that can be planned on a day
CONTAINERS = ("project", "big_ticket")   # sizes that only hold next steps
SIZE_LABELS = {"quick": "Quick", "task": "Task", "project": "Project", "big_ticket": "Big ticket"}


class RuleError(Exception):
    """A rule refused the change. The message is shown to the user."""


# ---------------------------------------------------------------- helpers

def get_item(db, item_id: int):
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise RuleError("That item no longer exists.")
    return row


def next_position(db) -> int:
    return db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM items").fetchone()[0]


def day_label(day: date) -> str:
    return f"{day:%A}"


def day_load(db, day: date, exclude_id: int = 0) -> dict:
    """How full a day is. `exclude_id` leaves one item out of the count."""
    rows = db.execute(
        "SELECT size, in_quick_block FROM items "
        "WHERE planned_date = ? AND status IN ('planned', 'done') AND id != ?",
        (day.isoformat(), exclude_id),
    ).fetchall()
    has_block = db.execute(
        "SELECT 1 FROM quick_blocks WHERE day = ?", (day.isoformat(),)
    ).fetchone() is not None
    tasks = sum(1 for r in rows if r["size"] == "task")
    return {
        "tasks": tasks + (1 if has_block else 0),
        "quicks": sum(1 for r in rows if r["size"] == "quick" and not r["in_quick_block"]),
        "block_quicks": sum(1 for r in rows if r["size"] == "quick" and r["in_quick_block"]),
        "has_block": has_block,
    }


def active_project_ids(db, exclude_item_id: int = 0) -> set:
    """Projects that have at least one planned, unfinished next step."""
    rows = db.execute(
        "SELECT DISTINCT parent_id FROM items "
        "WHERE status = 'planned' AND parent_id IS NOT NULL AND id != ?",
        (exclude_item_id,),
    ).fetchall()
    return {r["parent_id"] for r in rows}


def is_day_done(db, day: date) -> bool:
    """True when the day had planned items and every one of them is done."""
    row = db.execute(
        "SELECT SUM(status = 'planned') AS open, SUM(status = 'done') AS done "
        "FROM items WHERE planned_date = ?",
        (day.isoformat(),),
    ).fetchone()
    return (row["open"] or 0) == 0 and (row["done"] or 0) > 0


def today_is_closed(db, today: date) -> bool:
    """Rule 4: today is done and has not been reopened on purpose."""
    return is_day_done(db, today) and get_setting(db, "reopened_day") != today.isoformat()


def reopen_today(db, today: date) -> None:
    set_setting(db, "reopened_day", today.isoformat())


def _refuse_if_today_closed(db, day: date, today: date) -> None:
    if day == today and today_is_closed(db, today):
        raise RuleError("Today is done. Reopen today first if you really want to add more.")


# ---------------------------------------------------------------- capture

def capture(db, title: str, notes: str = "", source_ref: str | None = None) -> int:
    """Add a new item to the inbox. Only a title is needed."""
    title = (title or "").strip()
    if not title:
        raise RuleError("Type something to capture.")
    cur = db.execute(
        "INSERT INTO items (title, notes, status, position, source_ref, created_at) "
        "VALUES (?, ?, 'inbox', ?, ?, ?)",
        (title, (notes or "").strip(), next_position(db), source_ref, utc_now_text()),
    )
    return cur.lastrowid


# ---------------------------------------------------------------- planning

def plan_item(db, item_id: int, day: date, today: date, in_block: bool = False) -> None:
    """Put an item on a day. Enforces rules 1, 2 and 4."""
    item = get_item(db, item_id)
    if item["status"] in ("done", "dropped"):
        raise RuleError("That item is already finished or dropped.")
    if item["size"] in CONTAINERS:
        raise RuleError("Projects and big-ticket items can't be planned. Plan one of their next steps.")
    if item["size"] not in SCHEDULABLE:
        raise RuleError("Give it a size (quick or task) before planning it.")
    if day < today:
        raise RuleError("You can't plan something in the past.")

    already_here = item["status"] == "planned" and item["planned_date"] == day.isoformat()
    if not already_here:
        _refuse_if_today_closed(db, day, today)

    caps_load = day_load(db, day, exclude_id=item_id)
    name = day_label(day)
    if in_block:
        if item["size"] != "quick":
            raise RuleError("Only quick items go in a quick block.")
        if not caps_load["has_block"]:
            raise RuleError(f"{name} has no quick block yet.")
        size = get_int(db, "quick_block_size")
        if caps_load["block_quicks"] >= size:
            raise RuleError(f"{name}'s quick block is full ({size} of {size}). Remove one first.")
    elif item["size"] == "quick":
        cap = get_int(db, "quick_cap")
        if caps_load["quicks"] >= cap:
            raise RuleError(f"{name}'s quick lane is full ({cap} of {cap}). Remove one first.")
    else:
        cap = get_int(db, "task_cap")
        if caps_load["tasks"] >= cap:
            raise RuleError(f"{name} is full ({cap} of {cap} tasks). Remove one first.")

    if item["parent_id"]:
        active = active_project_ids(db, exclude_item_id=item_id)
        limit = get_int(db, "max_active_projects")
        if item["parent_id"] not in active and len(active) >= limit:
            raise RuleError(
                f"You already have {limit} active projects. "
                "Finish or unplan their steps before starting another."
            )

    db.execute(
        "UPDATE items SET status = 'planned', planned_date = ?, in_quick_block = ?, position = ? "
        "WHERE id = ?",
        (day.isoformat(), 1 if in_block else 0, next_position(db), item_id),
    )


def set_status(db, item_id: int, status: str) -> None:
    """Move an item to the inbox, backlog or someday. Takes it off any day."""
    if status not in ("inbox", "backlog", "someday"):
        raise RuleError("Unknown list.")
    get_item(db, item_id)
    db.execute(
        "UPDATE items SET status = ?, planned_date = NULL, in_quick_block = 0, "
        "completed_at = NULL, position = ? WHERE id = ?",
        (status, next_position(db), item_id),
    )


def add_quick_block(db, day: date, today: date) -> None:
    """Reserve one task slot on a day for a batch of quick items."""
    if day < today:
        raise RuleError("You can't plan something in the past.")
    _refuse_if_today_closed(db, day, today)
    current = day_load(db, day)
    if current["has_block"]:
        raise RuleError(f"{day_label(day)} already has a quick block.")
    cap = get_int(db, "task_cap")
    if current["tasks"] >= cap:
        raise RuleError(f"{day_label(day)} is full ({cap} of {cap} tasks). Remove one first.")
    db.execute("INSERT INTO quick_blocks (day) VALUES (?)", (day.isoformat(),))


def remove_quick_block(db, day: date) -> None:
    """Remove a day's quick block. Its unfinished quicks go back to the backlog."""
    finished = db.execute(
        "SELECT COUNT(*) FROM items WHERE planned_date = ? AND in_quick_block = 1 AND status = 'done'",
        (day.isoformat(),),
    ).fetchone()[0]
    if finished:
        raise RuleError("This quick block already has finished quicks, so it stays.")
    db.execute(
        "UPDATE items SET status = 'backlog', planned_date = NULL, in_quick_block = 0 "
        "WHERE planned_date = ? AND in_quick_block = 1 AND status = 'planned'",
        (day.isoformat(),),
    )
    db.execute("DELETE FROM quick_blocks WHERE day = ?", (day.isoformat(),))


# ---------------------------------------------------------------- finishing

def _drop_open_steps(db, parent_id: int) -> None:
    db.execute(
        "UPDATE items SET status = 'dropped', planned_date = NULL, in_quick_block = 0 "
        "WHERE parent_id = ? AND status NOT IN ('done', 'dropped')",
        (parent_id,),
    )


def complete_item(db, item_id: int) -> None:
    """Mark done. Finishing a project also drops its unfinished next steps."""
    item = get_item(db, item_id)
    db.execute(
        "UPDATE items SET status = 'done', completed_at = ? WHERE id = ?",
        (utc_now_text(), item_id),
    )
    if item["size"] in CONTAINERS:
        _drop_open_steps(db, item_id)


def reopen_item(db, item_id: int) -> None:
    """Undo done or dropped. It returns to its planned day if it had one, else the backlog."""
    item = get_item(db, item_id)
    status = "planned" if item["status"] == "done" and item["planned_date"] else "backlog"
    db.execute(
        "UPDATE items SET status = ?, completed_at = NULL WHERE id = ?", (status, item_id)
    )


def toggle_done(db, item_id: int) -> None:
    if get_item(db, item_id)["status"] == "done":
        reopen_item(db, item_id)
    else:
        complete_item(db, item_id)


def drop_item(db, item_id: int) -> None:
    """Decide not to do it. Dropping a project also drops its unfinished next steps."""
    item = get_item(db, item_id)
    db.execute(
        "UPDATE items SET status = 'dropped', planned_date = NULL, in_quick_block = 0, "
        "completed_at = ? WHERE id = ?",
        (utc_now_text(), item_id),
    )
    if item["size"] in CONTAINERS:
        _drop_open_steps(db, item_id)


# ---------------------------------------------------------------- editing

def update_item(db, item_id: int, title: str, notes: str, size: str | None,
                kind_id: int | None, hard_date: str | None, parent_id: int | None) -> None:
    item = get_item(db, item_id)
    title = (title or "").strip()
    if not title:
        raise RuleError("An item needs a title.")
    size = size or None
    if size is not None and size not in SIZES:
        raise RuleError("Unknown size.")
    parent_id = parent_id or None
    kind_id = kind_id or None
    hard_date = hard_date or None
    if hard_date:
        try:
            date.fromisoformat(hard_date)
        except ValueError:
            raise RuleError("The deadline is not a valid date.")

    if item["status"] == "planned" and (size != item["size"] or parent_id != item["parent_id"]):
        raise RuleError("Take it off its day before changing its size or project.")
    has_steps = db.execute(
        "SELECT 1 FROM items WHERE parent_id = ? LIMIT 1", (item_id,)
    ).fetchone() is not None
    if has_steps and size not in CONTAINERS:
        raise RuleError("This item has next steps, so it must stay a project or big ticket.")
    if size in CONTAINERS and parent_id:
        raise RuleError("A project can't be a next step of another project.")
    if parent_id:
        parent = db.execute("SELECT size FROM items WHERE id = ?", (parent_id,)).fetchone()
        if parent is None or parent["size"] not in CONTAINERS or parent_id == item_id:
            raise RuleError("Next steps can only belong to a project or big ticket.")
    if kind_id and not db.execute("SELECT 1 FROM kinds WHERE id = ?", (kind_id,)).fetchone():
        raise RuleError("Unknown kind.")

    db.execute(
        "UPDATE items SET title = ?, notes = ?, size = ?, kind_id = ?, hard_date = ?, parent_id = ? "
        "WHERE id = ?",
        (title, (notes or "").strip(), size, kind_id, hard_date, parent_id, item_id),
    )


def add_step(db, parent_id: int, title: str, size: str = "task") -> int:
    """Add a next step to a project. It starts unplanned, with the project's kind."""
    parent = get_item(db, parent_id)
    if parent["size"] not in CONTAINERS:
        raise RuleError("Only projects and big-ticket items have next steps.")
    title = (title or "").strip()
    if not title:
        raise RuleError("A next step needs a title.")
    if size not in SCHEDULABLE:
        raise RuleError("A next step must be a quick or a task.")
    cur = db.execute(
        "INSERT INTO items (title, size, kind_id, status, parent_id, position, created_at) "
        "VALUES (?, ?, ?, 'backlog', ?, ?, ?)",
        (title, size, parent["kind_id"], parent_id, next_position(db), utc_now_text()),
    )
    return cur.lastrowid


def reorder(db, table: str, ids: list) -> None:
    """Save a manual drag order for the rows in `ids` (listed in their new order).

    The rows swap the position numbers they already hold between themselves,
    so rows that are not in `ids` (hidden or filtered out) keep their place.
    """
    if table not in ("items", "kinds"):
        raise RuleError("Unknown list.")
    ids = [int(i) for i in ids]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = db.execute(f"SELECT position FROM {table} WHERE id IN ({marks})", ids).fetchall()
    positions = sorted(r["position"] for r in rows)
    if len(set(positions)) != len(ids):   # missing or duplicate numbers: start a clean run
        positions = list(range(1, len(ids) + 1))
    for position, row_id in zip(positions, ids):
        db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (position, row_id))


# ---------------------------------------------------------------- review (rule 3)

def overdue_items(db, today: date) -> list:
    """Unfinished items planned for a past day, oldest first."""
    return db.execute(
        "SELECT i.*, k.name AS kind_name, p.title AS parent_title FROM items i "
        "LEFT JOIN kinds k ON k.id = i.kind_id LEFT JOIN items p ON p.id = i.parent_id "
        "WHERE i.status = 'planned' AND i.planned_date < ? "
        "ORDER BY i.planned_date, i.position",
        (today.isoformat(),),
    ).fetchall()


def review_decide(db, item_id: int, action: str, today: date, day: date | None = None) -> None:
    """Decide one overdue item: 'today', 'pick' (with a day), 'backlog' or 'drop'."""
    item = get_item(db, item_id)
    if item["status"] != "planned" or not item["planned_date"] or item["planned_date"] >= today.isoformat():
        raise RuleError("That item doesn't need a decision.")
    if action == "drop":
        drop_item(db, item_id)
        return
    if action == "today":
        plan_item(db, item_id, today, today)
    elif action == "pick":
        if day is None:
            raise RuleError("Pick a day first.")
        plan_item(db, item_id, day, today)
    elif action == "backlog":
        set_status(db, item_id, "backlog")
    else:
        raise RuleError("Unknown choice.")
    db.execute("UPDATE items SET bump_count = bump_count + 1 WHERE id = ?", (item_id,))


# ---------------------------------------------------------------- kinds

def add_kind(db, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise RuleError("A kind needs a name.")
    if db.execute("SELECT 1 FROM kinds WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise RuleError("That kind already exists.")
    position = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM kinds").fetchone()[0]
    db.execute("INSERT INTO kinds (name, position) VALUES (?, ?)", (name, position))


def rename_kind(db, kind_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise RuleError("A kind needs a name.")
    clash = db.execute(
        "SELECT 1 FROM kinds WHERE name = ? COLLATE NOCASE AND id != ?", (name, kind_id)
    ).fetchone()
    if clash:
        raise RuleError("That kind already exists.")
    db.execute("UPDATE kinds SET name = ? WHERE id = ?", (name, kind_id))


def delete_kind(db, kind_id: int, move_to: int | None = None) -> None:
    """Delete a kind. If items still use it, they must be moved to another kind first."""
    in_use = db.execute("SELECT COUNT(*) FROM items WHERE kind_id = ?", (kind_id,)).fetchone()[0]
    if in_use:
        target = None
        if move_to and move_to != kind_id:
            target = db.execute("SELECT id FROM kinds WHERE id = ?", (move_to,)).fetchone()
        if target is None:
            raise RuleError(f"{in_use} items use this kind. Choose a kind to move them to first.")
        db.execute("UPDATE items SET kind_id = ? WHERE kind_id = ?", (move_to, kind_id))
    db.execute("DELETE FROM kinds WHERE id = ?", (kind_id,))
