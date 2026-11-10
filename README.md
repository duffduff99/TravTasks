# TravTasks

A small personal planner whose job is to enforce limits: a few tasks per day,
no automatic rollover, and a "Done for today" screen instead of an endless list.

One shared planner, one password. Python + FastAPI + SQLite + Jinja + HTMX, in one container.

## The rules

1. **Daily cap.** A day holds 3 tasks and a separate quick lane of 5. A *quick block* uses
   one task slot and holds up to 8 quicks. Finished items still use their slot. Over the
   cap is refused: remove something first.
2. **Projects are never scheduled.** Only their next steps are. At most 2 projects may have
   a planned step at once.
3. **No rollover.** Unfinished items from past days are shown one at a time
   (Today / Pick a day / Backlog / Drop). Nothing else opens until each is decided. Capture still works.
4. **Done for today.** When everything planned today is checked, Today shows only a done
   screen. Nothing can be added to today unless you tap "Reopen today".
5. No priorities, no tags, no due dates except a hard deadline.

All limits are numbers on the Settings page.

## Install (Dockhand or plain Docker)

In Dockhand, create a stack from this Git repository; it builds the `Dockerfile` through
`docker-compose.yml`. Or on any Docker host:

```bash
docker compose up -d --build
```

Open `http://<host>:8000`. The first visit asks you to choose the password.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | Port the app listens on. |
| `COOKIE_SECURE` | `false` | Set `true` once you only use the app over HTTPS. |

Data is one SQLite file, `travtasks.db`, on the `travtasks-data` volume. Back up that file.

**Forgot the password?** Clear it, then reload the app to choose a new one:

```bash
docker compose exec travtasks python -c "from app.db import connect; d = connect(); d.execute(\"DELETE FROM settings WHERE key='password_hash'\"); d.commit()"
```

## Home Assistant

The app does not need Home Assistant. To connect them:

- **Embed:** in Settings, set "Allow embedding from" to your Home Assistant address
  (for example `https://ha.example.com`), then add a Webpage card or dashboard pointing at the app.
  Both must be on the same parent domain and use HTTPS, or the login cookie will be blocked.
- **Sensor and capture** (the token is on the Settings page):

```yaml
rest:
  - resource: https://tasks.example.com/api/today
    headers:
      Authorization: "Bearer YOUR_TOKEN"
    scan_interval: 300
    sensor:
      - name: "Tasks open today"
        value_template: "{{ value_json.open_count }}"
        json_attributes: [open_tasks, open_quicks, done_count, inbox_count, done_for_today, needs_review_count]

rest_command:
  travtasks_capture:
    url: https://tasks.example.com/api/capture
    method: POST
    headers:
      Authorization: "Bearer YOUR_TOKEN"
    content_type: "application/json"
    payload: '{"title": "{{ title }}"}'
```

Phone shortcut: POST the same JSON to `/api/capture` with the same header.

## Calendars

Add ICS links on the Settings page. Each is either "event titles" or "only Busy".
They are fetched by the server every 15 minutes. A link is never shown again after saving,
never logged, and never sent to the browser. It is stored unencrypted in the SQLite file.

## Jira import (one time)

Export your issues from Jira as CSV, then:

```bash
docker cp export.csv travtasks:/data/export.csv
```

```bash
docker compose exec travtasks python -m scripts.import_jira /data/export.csv --dry-run
```

```bash
docker compose exec travtasks python -m scripts.import_jira /data/export.csv
```

Issue type "Quick" becomes size quick; status "Freezer" goes to Someday; everything else
lands in the inbox. The Jira key is kept on each item. Issues Jira counts as done are
skipped unless you add `--include-done`. Running it twice does not create duplicates.

## Working on the code

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/uvicorn app.main:app --reload
```

| File | What lives there |
|---|---|
| `app/rules.py` | **Every rule and every change to an item.** Start here. |
| `app/queries.py` | Read-only queries for each page. |
| `app/views.py` | Planner pages and actions. Routes call `rules.py`, nothing else. |
| `app/settings_views.py` | The Settings page. |
| `app/main.py` | App startup, login, `/api/*`, iframe header. |
| `app/web.py` | Shared helpers: database connection, login checks, flash messages. |
| `app/calendars.py` | ICS fetching and caching. |
| `app/clock.py` | What "today" means (4 AM rollover). |
| `app/db.py`, `app/schema.sql` | Tables, settings defaults, migrations. |
| `app/templates/`, `app/static/` | HTML, CSS and a small `app.js` for drag and drop. |
| `tests/test_rules.py` | One section of tests per rule. |

How to make common changes:

- **Change a rule:** edit the function in `rules.py`, raise `RuleError("message")` to refuse,
  and add a test in `tests/test_rules.py`.
- **Add a setting:** add a default in `DEFAULT_SETTINGS` (`db.py`), a field in
  `settings.html`, and save it in `settings_views.save_general`.
- **Change the database:** append a SQL string to `MIGRATIONS` in `db.py`. Never edit `schema.sql`.
- **Add a page:** add a route in `views.py` that calls `render(...)` with a new template
  that extends `base.html`.

htmx and SortableJS are vendored in `app/static/vendor/`; there is no build step.
