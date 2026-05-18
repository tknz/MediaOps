# MediaManager

MediaManager is a self-hosted media analytics and management app for Plex, Seerr, Radarr, Sonarr, SABnzbd, and Tautulli.

It combines live playback visibility, watch history, request moderation, download status, library cleanup, and storage intelligence in one operational interface.

## Features

- Plex sign-in with admin and standard user views
- Live playback sessions with bandwidth, transcode, device, and IP details
- User profiles with watch history, devices, IPs, request data, and streaming policies
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

## Configuration

Runtime configuration is stored outside the image in `/config`. Keep API keys and generated config files out of git.

Useful environment variables:

- `APP_NAME`: display name for the app
- `BASE_URL`: public URL used for auth callbacks
- `SECRET_KEY`: session signing secret
- `DATABASE_URL`: PostgreSQL connection string
- `PLEX_SERVER_URL` and `PLEX_SERVER_TOKEN`: Plex server access
- `SEERR_URL`, `RADARR_URL`, `SONARR_URL`, `SABNZBD_URL`, `TAUTULLI_URL`: service URLs
- `*_API_KEY`: service API keys
- `IMPORT_*_DB`: optional mounted SQLite databases for bootstrap imports

## Safety

Destructive library actions are admin-only and require confirmation in the UI. MediaManager talks to Plex, Seerr, Radarr, and Sonarr through their APIs; it does not modify those applications' databases directly.
