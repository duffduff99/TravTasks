"""Planner pages and the actions on them.

Routes only gather data and call rules.py. They never change items directly.
  planner: pages that are locked while the review (rule 3) is pending.
  always:  capture and the review itself, available whenever logged in.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from . import calendars, clock, queries, rules
from .web import (attempt, flash, get_db, go_back, parse_day, render, require_login,
                  require_planner, safe_path)

planner = APIRouter(dependencies=[Depends(require_planner)])
always = APIRouter(dependencies=[Depends(require_login)])


# ---------------------------------------------------------------- capture

@always.post("/capture")
def capture(request: Request, db=Depends(get_db), title: str = Form("")):
    if attempt(request, db, rules.capture, title):
        flash(request, f"Captured: {title.strip()}")
    if request.headers.get("hx-request") and not request.headers.get("hx-boosted"):
        # The capture box stays put; app.js reloads the page content below it.
        return Response(status_code=204, headers={"HX-Trigger": "refresh-main"})
    return go_back(request)


# ---------------------------------------------------------------- review (rule 3)

@always.get("/review")
def review_page(request: Request, db=Depends(get_db)):
    today = clock.today(db)
    overdue = rules.overdue_items(db, today)
    if not overdue:
        return RedirectResponse("/", status_code=303)
    item = overdue[0]
    load = rules.day_load(db, today)
    limits = queries.caps(db)
    if item["size"] == "quick":
        today_full = load["quicks"] >= limits["quick_cap"]
    else:
        today_full = load["tasks"] >= limits["task_cap"]
    return render(request, db, "review.html", item=item, remaining=len(overdue),
                  today_full=today_full or rules.today_is_closed(db, today),
                  bare=True, capture_box=True)


@always.post("/review/{item_id}")
def review_decide(request: Request, item_id: int, db=Depends(get_db),
                  action: str = Form(...), day: str = Form("")):
    attempt(request, db, rules.review_decide, item_id, action, clock.today(db), parse_day(day))
    return RedirectResponse("/review", status_code=303)


# ---------------------------------------------------------------- pages

@planner.get("/")
def overview(request: Request, db=Depends(get_db), inbox: str = ""):
    today = clock.today(db)
    inbox_items = queries.inbox(db)
    return render(
        request, db, "overview.html", nav="overview",
        days=queries.week(db, today),
        inbox_items=inbox_items if inbox == "all" else inbox_items[:6],
        inbox_total=len(inbox_items),
        projects=queries.active_projects(db),
        deadlines=queries.deadlines(db, today),
    )


def _day_page(request: Request, db, day: date):
    today = clock.today(db)
    events = calendars.events_by_day(db, day, day, clock.get_tz(db)).get(day, [])
    return render(request, db, "day.html", nav="today" if day == today else "week",
                  view=queries.day_view(db, day), events=events, is_today=day == today)


@planner.get("/today")
def today_page(request: Request, db=Depends(get_db)):
    today = clock.today(db)
    if rules.today_is_closed(db, today):
        # Rule 4: no backlog, no suggestions. Just the done screen.
        view = queries.day_view(db, today)
        return render(request, db, "done_today.html", nav="today",
                      tasks_done=len(view["tasks"]),
                      quicks_done=len(view["quicks"]) + len(view["block"]))
    return _day_page(request, db, today)


@planner.post("/today/reopen")
def today_reopen(request: Request, db=Depends(get_db)):
    rules.reopen_today(db, clock.today(db))
    return RedirectResponse("/today", status_code=303)


@planner.get("/day/{day}")
def day_page(request: Request, day: str, db=Depends(get_db)):
    chosen = parse_day(day)
    today = clock.today(db)
    if chosen is None or chosen <= today:
        return RedirectResponse("/today", status_code=303)
    return _day_page(request, db, chosen)


@planner.get("/week")
def week_page(request: Request, db=Depends(get_db), start: str = ""):
    today = clock.today(db)
    first = max(parse_day(start) or today, today)
    last = first + timedelta(days=6)
    return render(
        request, db, "week.html", nav="week", wide=True,
        days=queries.week(db, first),
        events=calendars.events_by_day(db, first, last, clock.get_tz(db)),
        plannable=queries.plannable(db),
        first=first, last=last,
        previous=max(first - timedelta(days=7), today) if first > today else None,
        following=first + timedelta(days=7),
    )


@planner.get("/backlog")
def backlog_page(request: Request, db=Depends(get_db), status: str = "backlog",
                 size: str = "", kind: int = 0):
    status = status if status in ("backlog", "someday") else "backlog"
    items = queries.listing(db, status, size, kind)
    project_ids = [i["id"] for i in items if i["size"] in rules.CONTAINERS]
    return render(
        request, db, "backlog.html", nav="backlog",
        status=status, size=size, kind=kind, items=items,
        steps=queries.steps_by_project(db, project_ids),
        active_ids=rules.active_project_ids(db),
        kinds=queries.kinds(db),
        sortable=not size and not kind,
    )


@planner.get("/done")
def done_page(request: Request, db=Depends(get_db), show: str = "done"):
    show = show if show in ("done", "dropped") else "done"
    return render(request, db, "done_log.html", nav="more", show=show,
                  groups=queries.done_log(db, show))


@planner.get("/more")
def more_page(request: Request, db=Depends(get_db)):
    return render(request, db, "more.html", nav="more")


@planner.get("/items/{item_id}")
def item_page(request: Request, item_id: int, db=Depends(get_db), next: str = ""):
    item = queries.item(db, item_id)
    if item is None:
        flash(request, "That item no longer exists.", "error")
        return RedirectResponse("/", status_code=303)
    today = clock.today(db)
    limits = queries.caps(db)
    # One button per day for the next week, showing how full the matching lane is.
    plan_days = []
    if item["size"] in rules.SCHEDULABLE and item["status"] not in ("done", "dropped"):
        for offset in range(7):
            day = today + timedelta(days=offset)
            load = rules.day_load(db, day, exclude_id=item_id)
            used, cap = ((load["quicks"], limits["quick_cap"]) if item["size"] == "quick"
                         else (load["tasks"], limits["task_cap"]))
            plan_days.append({
                "date": day, "used": used, "cap": cap, "full": used >= cap,
                "block": item["size"] == "quick" and load["has_block"],
                "block_used": load["block_quicks"],
                "block_full": load["block_quicks"] >= limits["quick_block_size"],
            })
    projects = db.execute(
        "SELECT id, title FROM items WHERE size IN ('project', 'big_ticket') "
        "AND status NOT IN ('done', 'dropped') AND id != ? ORDER BY title", (item_id,)
    ).fetchall()
    return render(
        request, db, "item.html", nav="", item=item, plan_days=plan_days,
        kinds=queries.kinds(db), projects=projects,
        steps=queries.steps_by_project(db, [item_id])[item_id],
        next=safe_path(next) or "/",
    )


# ---------------------------------------------------------------- item actions

@planner.post("/items/{item_id}/edit")
def item_edit(request: Request, item_id: int, db=Depends(get_db),
              title: str = Form(""), notes: str = Form(""), size: str = Form(""),
              kind_id: int = Form(0), hard_date: str = Form(""), parent_id: int = Form(0)):
    attempt(request, db, rules.update_item, item_id, title, notes, size, kind_id,
            hard_date, parent_id, success="Saved.")
    return go_back(request)


@planner.post("/items/{item_id}/plan")
def item_plan(request: Request, item_id: int, db=Depends(get_db),
              day: str = Form(""), in_block: int = Form(0), next: str = Form("")):
    chosen = parse_day(day)
    if chosen is None:
        flash(request, "Pick a day first.", "error")
        return go_back(request)
    ok = attempt(request, db, rules.plan_item, item_id, chosen, clock.today(db),
                 bool(in_block), success=f"Planned for {chosen:%A, %b} {chosen.day}.")
    return go_back(request, next if ok else None)


@planner.post("/items/{item_id}/status")
def item_status(request: Request, item_id: int, db=Depends(get_db),
                status: str = Form(...), next: str = Form("")):
    ok = attempt(request, db, rules.set_status, item_id, status, success=f"Moved to {status}.")
    return go_back(request, next if ok else None)


@planner.post("/items/{item_id}/toggle")
def item_toggle(request: Request, item_id: int, db=Depends(get_db), next: str = Form("")):
    attempt(request, db, rules.toggle_done, item_id)
    return go_back(request, next)


@planner.post("/items/{item_id}/drop")
def item_drop(request: Request, item_id: int, db=Depends(get_db), next: str = Form("")):
    attempt(request, db, rules.drop_item, item_id, success="Dropped.")
    return go_back(request, next)


@planner.post("/items/{item_id}/restore")
def item_restore(request: Request, item_id: int, db=Depends(get_db)):
    attempt(request, db, rules.reopen_item, item_id, success="Restored.")
    return go_back(request)


@planner.post("/items/{item_id}/steps")
def item_add_step(request: Request, item_id: int, db=Depends(get_db),
                  title: str = Form(""), size: str = Form("task")):
    attempt(request, db, rules.add_step, item_id, title, size)
    return go_back(request)


@planner.post("/day/{day}/block")
def block_add(request: Request, day: str, db=Depends(get_db)):
    chosen = parse_day(day)
    if chosen:
        attempt(request, db, rules.add_quick_block, chosen, clock.today(db))
    return go_back(request)


@planner.post("/day/{day}/block/remove")
def block_remove(request: Request, day: str, db=Depends(get_db)):
    chosen = parse_day(day)
    if chosen:
        attempt(request, db, rules.remove_quick_block, chosen)
    return go_back(request)


# ---------------------------------------------------------------- drag and drop

def _move(db, item_id: int, target: str, ids: list, today: date) -> None:
    """Apply one drag: put the item in the target list, then save that list's order.

    target is the data-list value of the list the item was dropped on:
      inbox | backlog | someday | steps | kinds
      day:YYYY-MM-DD            lane chosen by the item's size
      day:YYYY-MM-DD:tasks | :quick | :block
    """
    if target == "kinds":
        rules.reorder(db, "kinds", ids)
        return
    item = rules.get_item(db, item_id)
    if target in ("inbox", "backlog", "someday"):
        if item["status"] != target:
            rules.set_status(db, item_id, target)
    elif target.startswith("day:"):
        parts = target.split(":")
        day = parse_day(parts[1])
        lane = parts[2] if len(parts) > 2 else ""
        if day is None:
            raise rules.RuleError("Unknown day.")
        if lane == "tasks" and item["size"] == "quick":
            raise rules.RuleError("Quick items go in the quick lane or a quick block.")
        if lane in ("quick", "block") and item["size"] == "task":
            raise rules.RuleError("Only quick items go there. Tasks use a task slot.")
        in_block = lane == "block"
        in_place = (item["status"] == "planned" and item["planned_date"] == day.isoformat()
                    and bool(item["in_quick_block"]) == in_block)
        if not in_place:
            rules.plan_item(db, item_id, day, today, in_block)
    elif target != "steps":
        raise rules.RuleError("Unknown list.")
    rules.reorder(db, "items", ids)


@planner.post("/move")
def move(request: Request, db=Depends(get_db), payload: dict = Body(...)):
    """Called by app.js after a drag. The page reloads its content afterwards either way."""
    try:
        ids = [int(i) for i in payload.get("ids", [])]
        item_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return Response(status_code=400)
    attempt(request, db, _move, item_id, str(payload.get("target", "")), ids, clock.today(db))
    return Response(status_code=204)
