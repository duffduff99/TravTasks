"""One-time import of a Jira CSV export.

    python -m scripts.import_jira export.csv            (inside the container)
    python -m scripts.import_jira export.csv --dry-run  (show what would happen)

Mapping:
  Issue Type "Quick"   -> size quick
  Status "Freezer"     -> someday
  everything else      -> inbox, to triage by hand
  Issue key            -> source_ref (rows already imported are skipped)
Issues Jira already counts as done are skipped unless --include-done is given.
"""

import argparse
import csv
import sys

from app import rules
from app.clock import utc_now_text
from app.db import connect, init


def read_rows(path: str) -> list:
    """Jira exports repeat some column names, so look columns up by first position."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = [name.strip().lower() for name in next(reader)]

        def column(row, name):
            return row[header.index(name)].strip() if name in header and header.index(name) < len(row) else ""

        if "summary" not in header:
            sys.exit("This file has no 'Summary' column. Is it a Jira CSV export?")
        return [
            {
                "title": column(row, "summary"),
                "key": column(row, "issue key"),
                "type": column(row, "issue type"),
                "status": column(row, "status"),
                "category": column(row, "status category"),
                "notes": column(row, "description"),
            }
            for row in reader if row
        ]


def import_rows(db, rows: list, include_done: bool = False) -> dict:
    """Insert the rows. Returns how many went where."""
    result = {"inbox": 0, "someday": 0, "skipped_done": 0, "skipped_existing": 0, "skipped_blank": 0}
    for row in rows:
        if not row["title"]:
            result["skipped_blank"] += 1
            continue
        if row["key"] and db.execute(
            "SELECT 1 FROM items WHERE source_ref = ?", (row["key"],)
        ).fetchone():
            result["skipped_existing"] += 1
            continue
        is_done = row["category"].lower() == "done" or row["status"].lower() in ("done", "closed", "resolved")
        if is_done and not include_done:
            result["skipped_done"] += 1
            continue
        status = "someday" if row["status"].lower() == "freezer" else "inbox"
        size = "quick" if row["type"].lower() == "quick" else None
        db.execute(
            "INSERT INTO items (title, notes, size, status, position, source_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["title"], row["notes"], size, status, rules.next_position(db),
             row["key"] or None, utc_now_text()),
        )
        result[status] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a Jira CSV export into TravTasks.")
    parser.add_argument("csv_file")
    parser.add_argument("--include-done", action="store_true", help="also import issues Jira counts as done")
    parser.add_argument("--dry-run", action="store_true", help="show the counts without saving")
    args = parser.parse_args()

    db = connect()
    init(db)
    result = import_rows(db, read_rows(args.csv_file), args.include_done)
    if args.dry_run:
        db.rollback()
        print("Dry run, nothing saved.")
    else:
        db.commit()
    for name, count in result.items():
        print(f"{name.replace('_', ' ')}: {count}")


if __name__ == "__main__":
    main()
