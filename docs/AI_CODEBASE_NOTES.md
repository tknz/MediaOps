# MediaOps Codebase Map

This file is for fast orientation before editing. Keep it short and update it when moving major code.

## Runtime Shape

- `app/main.py` is the web app entry point. It currently owns route handlers, scheduler jobs, API payload shaping, and some queue/request helpers.
- `app/models.py` defines SQLAlchemy models.
- `app/db.py` creates the engine/session and applies small additive schema extensions at startup.
- `app/config.py` reads environment variables.
- `app/services/settings_store.py` reads and writes `/config/config.env`; values there override defaults, while environment variables override config file values.
- `app/templates/` contains Jinja server-rendered pages.
- `app/static/` contains browser JS and CSS.

## High-Traffic Features

- Live playback:
  - Plex API client: `app/services/clients.py`, `PlexClient.sessions()`
  - Active playback persistence: `app/services/plex_events.py`
  - Scheduled poll: `scheduled_plex_poll()` in `app/main.py`
  - Page/API/UI: `/live`, `/api/live`, `app/templates/live.html`, `app/static/live.js`

- Background operations:
  - Arr queue items and Plex background transcodes are normalized into `ActiveDownloadItem`.
  - Poller: `scheduled_downloads_poll()` in `app/main.py`
  - Plex background transcodes come from `/status/sessions/background`; they are not normal live playback sessions.

- Requests:
  - Seerr sync and request actions live mostly in `app/main.py`.
  - Enrichment helpers live in `app/services/request_intelligence.py`.
  - UI is `app/templates/downloads.html`.

- Library/media detail:
  - Browsing/enrichment logic is split between `app/main.py` and `app/services/libraries.py`.
  - Catalog sync is `app/services/library_catalog.py`.
  - UI has server-rendered data plus `app/static/libraries.js`.

- Settings/setup:
  - Settings form partial: `app/templates/settings_form.html`
  - Tab behavior and secret reveal: `app/static/settings-tabs.js`
  - Persistence: `app/services/settings_store.py`

## Good Future Splits

When there is time, split `app/main.py` into routers without changing behaviour:

- `app/routes/auth.py`: auth, logout, setup, Plex reconnect.
- `app/routes/settings.py`: settings, service tests, Tautulli import.
- `app/routes/live.py`: live page/API/terminate, background operation payloads.
- `app/routes/requests.py`: requests/downloads page and actions.
- `app/routes/users.py`: user list/detail, permissions, bans, policies.
- `app/routes/libraries.py`: libraries page/API/manage routes.
- `app/routes/graphs.py`: graphs/history/overview APIs.
- `app/scheduler.py`: scheduled jobs and interval configuration.

Do this incrementally, one router per commit. Keep payload helper functions near the router that uses them unless shared by multiple routers.

## Editing Rules

- Prefer adding parsing/normalisation to service modules instead of templates.
- Keep config values flowing through `all_settings()` unless a setting is truly environment-only.
- For new persistent fields, add both `models.py` and `ensure_schema_extensions()` so existing installs migrate.
- For live UI changes, update both the initial Jinja render and `app/static/live.js`.
- For cache-busted frontend assets, bump the query string in the template.
