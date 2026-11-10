"""Read-only queries that gather what each page shows. Nothing here changes data."""

from datetime import date, datetime, timedelta

from . import rules
from .clock import get_tz, planner_day
from .db import get_int

# Every item row comes with its kind name and its project's title.
ITEM_SELECT = (
    "SELECT i.*, k.name AS kind_name, p.title AS parent_title FROM items i "
    "LEFT JOIN kinds k ON k.id = i.kind_id LEFT JOIN items p ON p.id = i.parent_id "
)


def item(db, item_id: int):
    return db.execute(ITEM_SELECT + "WHERE i.id = ?", (item_id,)).fetchone()


def kinds(db) -> list:
    return db.execute(
        "SELECT k.*, (SELECT COUNT(*) FROM items WHERE kind_id = k.id) AS item_count "
        "FROM kinds k ORDER BY k.position, k.id"
    ).fetchall()


def caps(db) -> dict:
    return {
        "task_cap": get_int(db, "task_cap"),
        "quick_cap": get_int(db, "quick_cap"),
        "quick_block_size": get_int(db, "quick_block_size"),
        "max_active_projects": get_int(db, "max_active_projects"),
    }


def counts(db) -> dict:
    rows = db.execute(
        "SELECT status, COUNT(*) AS n FROM items WHERE parent_id IS NULL GROUP BY status"
    ).fetchall()
    found = {r["status"]: r["n"] for r in rows}
    return {s: found.get(s, 0) for s in ("inbox", "backlog", "someday")}


def day_view(db, day: date) -> dict:
    """Everything on one day, split into the three lists the Day page shows."""
    rows = db.execute(
        ITEM_SELECT + "WHERE i.planned_date = ? AND i.status IN ('planned', 'done') "
        "ORDER BY i.position, i.id",
        (day.isoformat(),),
    ).fetchall()
    return {
        "date": day,
        "load": rules.day_load(db, day),
        "tasks": [r for r in rows if r["size"] == "task"],
        "block": [r for r in rows if r["size"] == "quick" and r["in_quick_block"]],
        "quicks": [r for r in rows if r["size"] == "quick" and not r["in_quick_block"]],
    }


def week(db, start: date, days: int = 7) -> list:
    return [day_view(db, start + timedelta(days=n)) for n in range(days)]


def inbox(db) -> list:
    return db.execute(
        ITEM_SELECT + "WHERE i.status = 'inbox' ORDER BY i.position, i.id"
    ).fetchall()


def listing(db, status: str, size: str = "", kind_id: int = 0) -> list:
    """Top-level items in the backlog or someday list. Next steps are shown under their project."""
    sql = ITEM_SELECT + "WHERE i.status = ? AND i.parent_id IS NULL"
    args: list = [status]
    if size:
        sql += " AND i.size = ?"
        args.append(size)
    if kind_id:
        sql += " AND i.kind_id = ?"
        args.append(kind_id)
    return db.execute(sql + " ORDER BY i.position, i.id", args).fetchall()


def steps_by_project(db, project_ids: list) -> dict:
    """Unfinished next steps for each project id."""
    result = {pid: [] for pid in project_ids}
    if not project_ids:
        return result
    marks = ",".join("?" * len(project_ids))
    rows = db.execute(
        ITEM_SELECT + f"WHERE i.parent_id IN ({marks}) AND i.status NOT IN ('done', 'dropped') "
        "ORDER BY (i.planned_date IS NULL), i.planned_date, i.position, i.id",
        project_ids,
    ).fetchall()
    for row in rows:
        result[row["parent_id"]].append(row)
    return result


def plannable(db) -> list:
    """Backlog items that can be dragged onto a day (quick or task, steps included)."""
    return db.execute(
        ITEM_SELECT + "WHERE i.status = 'backlog' AND i.size IN ('quick', 'task') "
        "ORDER BY i.position, i.id"
    ).fetchall()


def active_projects(db) -> list:
    """Projects with a planned step, each with its soonest planned step."""
    ids = sorted(rules.active_project_ids(db))
    steps = steps_by_project(db, ids)
    projects = []
    for pid in ids:
        project = item(db, pid)
        planned = [s for s in steps[pid] if s["status"] == "planned"]
        if project:
            projects.append({"project": project, "next": planned[0] if planned else None})
    return projects


def deadlines(db, today: date, days: int = 14) -> list:
    """Open items whose hard date is past or within the next `days` days."""
    limit = (today + timedelta(days=days)).isoformat()
    return db.execute(
        ITEM_SELECT + "WHERE i.hard_date IS NOT NULL AND i.hard_date <= ? "
        "AND i.status NOT IN ('done', 'dropped') ORDER BY i.hard_date",
        (limit,),
    ).fetchall()


def done_log(db, status: str = "done", limit: int = 200) -> list:
    """Finished (or dropped) items, newest first, grouped by planner day."""
    rows = db.execute(
        ITEM_SELECT + "WHERE i.status = ? ORDER BY i.completed_at DESC, i.id DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    tz = get_tz(db)
    rollover = get_int(db, "rollover_hour")
    groups: list = []
    for row in rows:
        day = None
        if row["completed_at"]:
            day = planner_day(datetime.fromisoformat(row["completed_at"]), tz, rollover)
        if not groups or groups[-1]["date"] != day:
            groups.append({"date": day, "items": []})
        groups[-1]["items"].append(row)
    return groups


def today_summary(db, today: date) -> dict:
    """The JSON served at /api/today for Home Assistant."""
    view = day_view(db, today)
    limits = caps(db)
    planned = view["tasks"] + view["block"] + view["quicks"]
    open_items = [r for r in planned if r["status"] == "planned"]
    return {
        "date": today.isoformat(),
        "open_count": len(open_items),
        "done_count": len(planned) - len(open_items),
        "done_for_today": rules.is_day_done(db, today),
        "task_slots_used": view["load"]["tasks"],
        "task_cap": limits["task_cap"],
        "quick_slots_used": view["load"]["quicks"],
        "quick_cap": limits["quick_cap"],
        "has_quick_block": view["load"]["has_block"],
        "open_tasks": [r["title"] for r in view["tasks"] if r["status"] == "planned"],
        "open_quicks": [r["title"] for r in view["block"] + view["quicks"] if r["status"] == "planned"],
        "inbox_count": counts(db)["inbox"],
        "needs_review_count": len(rules.overdue_items(db, today)),
    }
