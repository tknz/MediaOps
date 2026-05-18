# MediaOps

MediaOps is a self-hosted media analytics and management app for Plex, Seerr, Radarr, Sonarr, SABnzbd, and Tautulli.

It combines live playback visibility, watch history, request moderation, download status, library cleanup, and storage intelligence in one operational interface.

## Features

- Plex sign-in with admin and standard user views
- Background Plex live-session collection with bandwidth, transcode, device, and IP details
- User profiles with watch history, devices, IPs, request data, and streaming policies
- User-bound IP, device, and streaming bans for moderation
- Seerr request review with approve, decline, delete, quota, and requester views
- Radarr and Sonarr inventory views for storage, stale media, unwatched media, monitoring, and delete actions
- Historical analytics backed by PostgreSQL
- Optional Tautulli bootstrap import for existing watch history

## Quick Start

1. Copy the example environment file:

   ```sh
   cp .env.example .env
   ```

2. Edit `.env` with your Plex, Seerr, Radarr, Sonarr, SABnzbd, and Tautulli details.

3. Start the app:

   ```sh
   docker compose up -d --build
   ```

4. Open:

   ```text
   http://localhost:8000
   ```

On first run, MediaOps opens the setup screens without Plex sign-in when `SETUP_NO_AUTH=true`. Use that to add service URLs and API keys, then connect Plex from the setup page. After Plex is connected, normal Plex sign-in protects the app.

## Configuration

Runtime configuration is stored outside the image in `/config`. Keep API keys and generated config files out of git.

Useful environment variables:

- `APP_NAME`: display name for the app
- `BASE_URL`: public URL used for auth callbacks
- `SECRET_KEY`: session signing secret
- `DATABASE_URL`: PostgreSQL connection string; overrides the `DB_*` fields when set
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSLMODE`: friendly PostgreSQL settings for Unraid templates and non-Compose installs
- `PLEX_SERVER_URL` and `PLEX_SERVER_TOKEN`: Plex server access
- `SEERR_URL`, `RADARR_URL`, `SONARR_URL`, `SABNZBD_URL`, `TAUTULLI_URL`: service URLs
- `*_API_KEY`: service API keys
- `SETUP_NO_AUTH`: allow first-run setup before Plex auth is connected; defaults to `true`
- `SETUP_USER`: temporary local setup admin username while Plex is unconfigured
- `IMPORT_*_DB`: optional one-time bootstrap import paths

## Database

MediaOps requires PostgreSQL. The included Compose file runs a `postgres:16-alpine` service named `db`, so the default connection uses Docker DNS:

```text
postgresql+psycopg://mediaops:mediaops@db:5432/mediaops
```

For Unraid or other single-container installs, run PostgreSQL separately and configure MediaOps with the friendly fields:

```env
DB_HOST=192.168.1.50
DB_PORT=5432
DB_NAME=mediaops
DB_USER=mediaops
DB_PASSWORD=change-me
DB_SSLMODE=
```

Advanced installs can provide `DATABASE_URL` directly instead:

```env
DATABASE_URL=postgresql+psycopg://mediaops:change-me@192.168.1.50:5432/mediaops
```

MediaOps keeps live-session state, watch history, request history, library analytics, and scheduled ingest data in PostgreSQL.

## Images

Publish the app image separately from PostgreSQL. A public deployment should run MediaOps plus a PostgreSQL container or an existing PostgreSQL server.

## Safety

Destructive library actions are admin-only and require confirmation in the UI. MediaOps talks to Plex, Seerr, Radarr, and Sonarr through their APIs; it does not modify those applications' databases directly.
