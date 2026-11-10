"""Tests for the planner rules. One section per rule in app/rules.py."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import clock, rules
from app.db import set_setting
from app.rules import RuleError

from .conftest import TODAY

TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def status_of(db, item_id):
    return rules.get_item(db, item_id)["status"]


# ---------------------------------------------------------------- the 4 AM day

def test_day_rolls_over_at_4am_chicago():
    tz = ZoneInfo("America/Chicago")
    assert clock.planner_day(datetime(2026, 11, 11, 3, 59, tzinfo=tz), tz, 4) == TODAY
    assert clock.planner_day(datetime(2026, 11, 11, 4, 0, tzinfo=tz), tz, 4) == TOMORROW


def test_day_uses_chicago_time_not_utc(db):
    # 08:00 UTC on Nov 11 is 2:00 AM in Chicago: still the planner day Nov 10.
    assert clock.today(db, datetime(2026, 11, 11, 8, 0, tzinfo=ZoneInfo("UTC"))) == TODAY


# ---------------------------------------------------------------- capture

def test_capture_needs_only_a_title_and_goes_to_inbox(db):
    item = rules.get_item(db, rules.capture(db, "  Fix the gate  "))
    assert (item["title"], item["status"], item["size"]) == ("Fix the gate", "inbox", None)


def test_capture_refuses_blank_title(db):
    with pytest.raises(RuleError):
        rules.capture(db, "   ")


# ---------------------------------------------------------------- rule 1: daily cap

def test_fourth_task_is_refused(db, make):
    for n in range(3):
        rules.plan_item(db, make(f"Task {n}"), TODAY, TODAY)
    extra = make("One too many")
    with pytest.raises(RuleError, match="full"):
        rules.plan_item(db, extra, TODAY, TODAY)
    assert status_of(db, extra) == "backlog"


def test_removing_one_task_makes_room(db, make):
    tasks = [make(f"Task {n}") for n in range(3)]
    for task in tasks:
        rules.plan_item(db, task, TODAY, TODAY)
    rules.set_status(db, tasks[0], "backlog")
    rules.plan_item(db, make("Replacement"), TODAY, TODAY)


def test_sixth_quick_is_refused(db, make):
    for n in range(5):
        rules.plan_item(db, make(f"Quick {n}", size="quick"), TODAY, TODAY)
    with pytest.raises(RuleError, match="quick lane is full"):
        rules.plan_item(db, make("Sixth", size="quick"), TODAY, TODAY)


def test_task_and_quick_lanes_are_separate(db, make):
    for n in range(3):
        rules.plan_item(db, make(f"Task {n}"), TODAY, TODAY)
    rules.plan_item(db, make("Quick", size="quick"), TODAY, TODAY)   # still fits


def test_finished_items_still_use_their_slot(db, make):
    tasks = [make(f"Task {n}") for n in range(3)]
    for task in tasks:
        rules.plan_item(db, task, TOMORROW, TODAY)
    rules.complete_item(db, tasks[0])
    with pytest.raises(RuleError):
        rules.plan_item(db, make("Sneaky"), TOMORROW, TODAY)


def test_caps_are_configurable(db, make):
    set_setting(db, "task_cap", 1)
    rules.plan_item(db, make("Only one"), TODAY, TODAY)
    with pytest.raises(RuleError):
        rules.plan_item(db, make("Second"), TODAY, TODAY)


def test_cap_applies_to_each_day_separately(db, make):
    for n in range(3):
        rules.plan_item(db, make(f"Task {n}"), TODAY, TODAY)
    rules.plan_item(db, make("Tomorrow"), TOMORROW, TODAY)


def test_cannot_plan_in_the_past(db, make):
    with pytest.raises(RuleError, match="past"):
        rules.plan_item(db, make(), YESTERDAY, TODAY)


def test_unsized_item_cannot_be_planned(db):
    with pytest.raises(RuleError, match="size"):
        rules.plan_item(db, rules.capture(db, "Not sized"), TODAY, TODAY)


# ---------------------------------------------------------------- rule 1: quick block

def test_quick_block_uses_one_task_slot(db, make):
    rules.add_quick_block(db, TODAY, TODAY)
    rules.plan_item(db, make("Task 1"), TODAY, TODAY)
    rules.plan_item(db, make("Task 2"), TODAY, TODAY)
    with pytest.raises(RuleError, match="full"):
        rules.plan_item(db, make("Task 3"), TODAY, TODAY)


def test_quick_block_refused_when_tasks_are_full(db, make):
    for n in range(3):
        rules.plan_item(db, make(f"Task {n}"), TODAY, TODAY)
    with pytest.raises(RuleError, match="full"):
        rules.add_quick_block(db, TODAY, TODAY)


def test_only_one_quick_block_per_day(db):
    rules.add_quick_block(db, TODAY, TODAY)
    with pytest.raises(RuleError, match="already"):
        rules.add_quick_block(db, TODAY, TODAY)


def test_block_quicks_do_not_count_against_quick_lane(db, make):
    rules.add_quick_block(db, TODAY, TODAY)
    for n in range(8):
        rules.plan_item(db, make(f"Block {n}", size="quick"), TODAY, TODAY, in_block=True)
    for n in range(5):
        rules.plan_item(db, make(f"Lane {n}", size="quick"), TODAY, TODAY)
    load = rules.day_load(db, TODAY)
    assert (load["block_quicks"], load["quicks"]) == (8, 5)


def test_quick_block_has_its_own_limit(db, make):
    set_setting(db, "quick_block_size", 2)
    rules.add_quick_block(db, TODAY, TODAY)
    for n in range(2):
        rules.plan_item(db, make(f"Block {n}", size="quick"), TODAY, TODAY, in_block=True)
    with pytest.raises(RuleError, match="block is full"):
        rules.plan_item(db, make("Third", size="quick"), TODAY, TODAY, in_block=True)


def test_block_needs_to_exist_and_takes_only_quicks(db, make):
    with pytest.raises(RuleError, match="no quick block"):
        rules.plan_item(db, make(size="quick"), TODAY, TODAY, in_block=True)
    rules.add_quick_block(db, TODAY, TODAY)
    with pytest.raises(RuleError, match="Only quick"):
        rules.plan_item(db, make(size="task"), TODAY, TODAY, in_block=True)


def test_removing_block_returns_its_quicks_to_backlog(db, make):
    rules.add_quick_block(db, TODAY, TODAY)
    quick = make(size="quick")
    rules.plan_item(db, quick, TODAY, TODAY, in_block=True)
    rules.remove_quick_block(db, TODAY)
    assert status_of(db, quick) == "backlog"
    assert rules.day_load(db, TODAY)["tasks"] == 0


def test_block_with_finished_quicks_cannot_be_removed(db, make):
    rules.add_quick_block(db, TODAY, TODAY)
    quick = make(size="quick")
    rules.plan_item(db, quick, TODAY, TODAY, in_block=True)
    rules.complete_item(db, quick)
    with pytest.raises(RuleError):
        rules.remove_quick_block(db, TODAY)


# ---------------------------------------------------------------- rule 2: projects

@pytest.mark.parametrize("size", ["project", "big_ticket"])
def test_projects_and_big_tickets_cannot_be_planned(db, make, size):
    with pytest.raises(RuleError, match="can't be planned"):
        rules.plan_item(db, make(size=size), TODAY, TODAY)


def test_a_projects_next_step_can_be_planned(db, make):
    project = make("Shelving", size="project")
    step = rules.add_step(db, project, "Buy brackets")
    rules.plan_item(db, step, TODAY, TODAY)
    assert rules.active_project_ids(db) == {project}


def test_third_active_project_is_refused(db, make):
    steps = []
    for n in range(3):
        project = make(f"Project {n}", size="project")
        steps.append(rules.add_step(db, project, f"Step {n}"))
    rules.plan_item(db, steps[0], TODAY, TODAY)
    rules.plan_item(db, steps[1], TODAY, TODAY)
    with pytest.raises(RuleError, match="active projects"):
        rules.plan_item(db, steps[2], TOMORROW, TODAY)


def test_more_steps_of_an_active_project_are_fine(db, make):
    first = make("Project A", size="project")
    second = make("Project B", size="project")
    rules.plan_item(db, rules.add_step(db, first, "A1"), TODAY, TODAY)
    rules.plan_item(db, rules.add_step(db, second, "B1"), TODAY, TODAY)
    rules.plan_item(db, rules.add_step(db, first, "A2"), TOMORROW, TODAY)


def test_finishing_a_step_frees_the_project_slot(db, make):
    steps = []
    for n in range(3):
        steps.append(rules.add_step(db, make(f"Project {n}", size="project"), f"Step {n}"))
    rules.plan_item(db, steps[0], TODAY, TODAY)
    rules.plan_item(db, steps[1], TODAY, TODAY)
    rules.complete_item(db, steps[0])
    rules.plan_item(db, steps[2], TOMORROW, TODAY)


def test_planned_item_cannot_be_turned_into_a_project(db, make):
    task = make()
    rules.plan_item(db, task, TODAY, TODAY)
    with pytest.raises(RuleError, match="Take it off"):
        rules.update_item(db, task, "Item", "", "project", None, None, None)


def test_steps_only_belong_to_projects(db, make):
    with pytest.raises(RuleError):
        rules.add_step(db, make(size="task"), "Nope")
    with pytest.raises(RuleError):
        rules.update_item(db, make("Child"), "Child", "", "task", None, None, make("Plain task"))


def test_dropping_a_project_drops_its_open_steps(db, make):
    project = make(size="project")
    step = rules.add_step(db, project, "Step")
    rules.plan_item(db, step, TODAY, TODAY)
    rules.drop_item(db, project)
    assert status_of(db, step) == "dropped"
    assert rules.day_load(db, TODAY)["tasks"] == 0


# ---------------------------------------------------------------- rule 3: no rollover

def plan_yesterday(db, make, title="Leftover", size="task"):
    item = make(title, size=size)
    rules.plan_item(db, item, YESTERDAY, YESTERDAY)
    return item


def test_unfinished_items_do_not_move_by_themselves(db, make):
    item = plan_yesterday(db, make)
    assert rules.get_item(db, item)["planned_date"] == YESTERDAY.isoformat()
    assert rules.day_load(db, TODAY)["tasks"] == 0
    assert [r["id"] for r in rules.overdue_items(db, TODAY)] == [item]


def test_review_lists_all_past_days_oldest_first(db, make):
    older = make("Older")
    rules.plan_item(db, older, TODAY - timedelta(days=3), TODAY - timedelta(days=3))
    newer = plan_yesterday(db, make, "Newer")
    done = plan_yesterday(db, make, "Finished")
    rules.complete_item(db, done)
    assert [r["id"] for r in rules.overdue_items(db, TODAY)] == [older, newer]


def test_review_today_moves_it_and_counts_a_bump(db, make):
    item = plan_yesterday(db, make)
    rules.review_decide(db, item, "today", TODAY)
    row = rules.get_item(db, item)
    assert (row["planned_date"], row["bump_count"]) == (TODAY.isoformat(), 1)
    assert rules.overdue_items(db, TODAY) == []


def test_review_today_is_refused_when_today_is_full(db, make):
    item = plan_yesterday(db, make)
    for n in range(3):
        rules.plan_item(db, make(f"Task {n}"), TODAY, TODAY)
    with pytest.raises(RuleError, match="full"):
        rules.review_decide(db, item, "today", TODAY)
    assert rules.get_item(db, item)["bump_count"] == 0


def test_review_pick_a_day(db, make):
    item = plan_yesterday(db, make)
    rules.review_decide(db, item, "pick", TODAY, TOMORROW)
    assert rules.get_item(db, item)["planned_date"] == TOMORROW.isoformat()
    with pytest.raises(RuleError):
        rules.review_decide(db, plan_yesterday(db, make, "Other"), "pick", TODAY, None)


def test_review_backlog_counts_a_bump(db, make):
    item = plan_yesterday(db, make)
    rules.review_decide(db, item, "backlog", TODAY)
    row = rules.get_item(db, item)
    assert (row["status"], row["planned_date"], row["bump_count"]) == ("backlog", None, 1)


def test_review_drop(db, make):
    item = plan_yesterday(db, make)
    rules.review_decide(db, item, "drop", TODAY)
    assert status_of(db, item) == "dropped"


def test_review_only_decides_overdue_items(db, make):
    item = make()
    rules.plan_item(db, item, TODAY, TODAY)
    with pytest.raises(RuleError):
        rules.review_decide(db, item, "drop", TODAY)


# ---------------------------------------------------------------- rule 4: done for today

def test_day_is_done_when_everything_planned_is_checked(db, make):
    task, quick = make(), make(size="quick")
    rules.plan_item(db, task, TODAY, TODAY)
    rules.plan_item(db, quick, TODAY, TODAY)
    rules.complete_item(db, task)
    assert not rules.is_day_done(db, TODAY)
    rules.complete_item(db, quick)
    assert rules.is_day_done(db, TODAY)


def test_empty_day_is_not_done(db):
    assert not rules.is_day_done(db, TODAY)


def test_nothing_can_be_added_once_today_is_done(db, make):
    task = make()
    rules.plan_item(db, task, TODAY, TODAY)
    rules.complete_item(db, task)
    with pytest.raises(RuleError, match="Today is done"):
        rules.plan_item(db, make("More work"), TODAY, TODAY)
    with pytest.raises(RuleError, match="Today is done"):
        rules.add_quick_block(db, TODAY, TODAY)
    rules.plan_item(db, make("For tomorrow"), TOMORROW, TODAY)   # other days stay open


def test_reopening_today_allows_more_up_to_the_cap(db, make):
    task = make()
    rules.plan_item(db, task, TODAY, TODAY)
    rules.complete_item(db, task)
    rules.reopen_today(db, TODAY)
    rules.plan_item(db, make("Second"), TODAY, TODAY)
    rules.plan_item(db, make("Third"), TODAY, TODAY)
    with pytest.raises(RuleError, match="full"):
        rules.plan_item(db, make("Fourth"), TODAY, TODAY)


def test_unchecking_returns_item_to_its_day(db, make):
    task = make()
    rules.plan_item(db, task, TODAY, TODAY)
    rules.toggle_done(db, task)
    assert status_of(db, task) == "done"
    rules.toggle_done(db, task)
    assert status_of(db, task) == "planned"


# ---------------------------------------------------------------- kinds and ordering

def kind_id(db, name):
    return db.execute("SELECT id FROM kinds WHERE name = ?", (name,)).fetchone()["id"]


def test_default_kinds_exist(db):
    names = [r["name"] for r in db.execute("SELECT name FROM kinds ORDER BY position")]
    assert names == ["Protect house", "Home upgrade", "Fun build", "Day to day", "Admin"]


def test_add_and_rename_kind(db):
    rules.add_kind(db, "Garden")
    rules.rename_kind(db, kind_id(db, "Garden"), "Yard")
    assert kind_id(db, "Yard")
    with pytest.raises(RuleError):
        rules.add_kind(db, "yard")


def test_deleting_a_kind_in_use_needs_a_new_home_for_its_items(db, make):
    admin, upgrade = kind_id(db, "Admin"), kind_id(db, "Home upgrade")
    item = make()
    rules.update_item(db, item, "Item", "", "task", admin, None, None)
    with pytest.raises(RuleError, match="move them to"):
        rules.delete_kind(db, admin)
    rules.delete_kind(db, admin, move_to=upgrade)
    assert rules.get_item(db, item)["kind_id"] == upgrade


def test_deleting_an_unused_kind_just_works(db):
    rules.delete_kind(db, kind_id(db, "Fun build"))
    assert db.execute("SELECT COUNT(*) FROM kinds").fetchone()[0] == 4


def test_reorder_keeps_hidden_items_in_place(db, make):
    a, b, c, d = (make(t) for t in "ABCD")
    rules.reorder(db, "items", [c, a])    # b and d were not part of the drag
    order = [r["id"] for r in db.execute("SELECT id FROM items ORDER BY position")]
    assert order == [c, b, a, d]
