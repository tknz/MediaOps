# MediaOps

MediaOps is a self-hosted operations view for Plex and the common tools around it. Seerr, Radarr, Sonarr, SABnzbd, and Tautulli.

It is developed for the admin the person running the server. It helps answer key questions in one tool: who is streaming, who requested what, which accounts look shared, what is downloading, and which media is taking space without being watched.

MediaOps is an initial beta. It is useful in beta, but it will still have bugs, take caution with some features, especially around delete and unmonitor actions.

## What It Does

**Live operations:** See active Plex streams with user, title, player, device, IP address, public reach, bandwidth, transcode decision, and download activity. An admin can terminate a stream or create a user-bound IP/device ban from the live session.

**Users and moderation:** Open a user profile to see watch history, graphs, devices, IP addresses, requests, watchlist items, Seerr permissions, quotas, streaming policy, and bans. Policies can cap concurrent streams, public IP count, and device count. Bans can target one user's IP or one Plex client/device, with a custom message.

**Requests and downloads:** Combine data from Seerr requests with Radarr/Sonarr data. See requester, status, expected release, release ETA, fulfilled size, queue progress, and available actions. Approve, decline, search, unmonitor, delete files through Arr, or delete only the request record.

**Libraries and storage:** Pull Radarr/Sonarr inventory, enrich it with watch history, and find large, stale, or unwatched media. Selecting a movie or show opens a focused detail view with storage, monitoring state, watchers, recent plays, and Arr actions.

**History and graphs:** Store completed Plex sessions in PostgreSQL, optionally pull historical data by connecting Tautulli history through its API, and graph usage by period, user, title, playback decision, platform, hour, weekday, bandwidth, and request behavior.

## How It Works

Plex and PostgreSQL are the core. MediaOps polls Plex for live sessions, turns stopped sessions into history rows, and stores app state in PostgreSQL. Plex sign-in is used for normal auth, and local auth is kept as an admin recovery path.

Optional integrations add more context:

| Integration | What MediaOps uses it for |
| --- | --- |
| Seerr | Requests, approvals, declines, request permissions, requester history |
| Radarr | Movie inventory, release/search data, queue state, unmonitor, delete files |
| Sonarr | TV inventory, release/search data, queue state, unmonitor, delete files |
| SABnzbd | Queue and download health visibility |
| Tautulli | One-time history import through the API and bandwidth backfill |

MediaOps uses service APIs. It does not write directly to Plex, Seerr, Radarr, Sonarr, SABnzbd, or Tautulli databases.

## Why It Exists

Each media app knows one part of the story. Plex knows what is playing. Seerr knows what was requested. Radarr and Sonarr know what exists, what is monitored, what size it is, and what can be searched or removed. Tautulli may hold old history.

MediaOps joins those pieces into one admin workflow for user support, account-sharing checks, request approval, and storage cleanup.

## Fastest Setup

You need Docker Compose, a Plex server you administer, and a persistent `/config` volume.

```sh
git clone https://github.com/tknz/MediaOps.git
cd MediaOps
cp .env.example .env
```

Generate secrets:

```sh
openssl rand -hex 32
openssl rand -hex 24
```

Edit `.env` and set the minimum values:

```env
BASE_URL=http://localhost:8000
SECRET_KEY=<first-random-value>
DB_PASSWORD=<second-random-value>
```

Start MediaOps and PostgreSQL:

```sh
docker compose up -d
```

Open `http://localhost:8000`, then:

1. Create local auth.
2. Sign in with Plex.
3. Choose the Plex server.
4. Connect optional services in Settings.

If you are behind a reverse proxy (I recommend Traefik or similar for TLS termination with the media-stack in its own docker network), set `BASE_URL` to the final public URL before using Plex sign-in:

```env
BASE_URL=https://mediaops.example.com
```

If `BASE_URL` does not match the browser URL, MediaOps may block settings saves as cross-origin requests.

## Deployment Approaches

**Recommended:** Use the included `docker-compose.yml`. It runs `tknz/mediaops:latest` plus `postgres:16-alpine`, with persistent volumes for `/config` and PostgreSQL.

For most installs, keep only startup basics in `.env` and finish service setup in the web UI:

```env
BASE_URL=https://your-mediaops-url.example.com
SECRET_KEY=<long-random-value>
DB_PASSWORD=<long-random-value>
```

**Environment-owned setup:** If your host manages container variables, put service URLs and API keys in `.env` as well:

```env
PLEX_SERVER_URL=http://plex:32400
PLEX_SERVER_TOKEN=...
SEERR_URL=http://seerr:5055
SEERR_API_KEY=...
RADARR_URL=http://radarr:7878
RADARR_API_KEY=...
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=...
```

Fields set by environment variables are read-only in Settings. Environment always wins over the writable config file.

**Existing PostgreSQL:** Run only the app container and provide `DATABASE_URL`:

```sh
docker run -d \
  --name mediaops \
  --restart unless-stopped \
  -p 8000:8000 \
  -v mediaops_config:/config \
  -e BASE_URL=https://mediaops.example.com \
  -e SECRET_KEY=<long-random-value> \
  -e DATABASE_URL=postgresql+psycopg://mediaops:<db-password>@postgres.example.com:5432/mediaops \
  tknz/mediaops:latest
```

## Configuration Rules

MediaOps has two configuration sources:

1. Container environment variables.
2. `/config/config.env`, written by Setup and Settings.

Use environment variables for values your platform owns. Use the web UI for values you want to manage inside MediaOps.

`/config/config.env` is plain `KEY=value` text. It is not JSON, indentation does not matter, and normal setup should not require editing it.

`PLEX_OWNER_ID` is normally not needed. MediaOps can infer it during Plex setup or from the connected Plex token.

For multi-instance Radarr/Sonarr setups, the Settings page writes numbered keys such as:

```env
RADARR_1_NAME=Radarr
RADARR_1_URL=http://radarr:7878
RADARR_1_API_KEY=...
RADARR_2_NAME=Radarr 4K
RADARR_2_URL=http://radarr-4k:7878
RADARR_2_API_KEY=...
```

Advanced environment-only installs can use `RADARR_INSTANCES` and `SONARR_INSTANCES` JSON, but the normal path is the Settings page.

## Security And Privacy

MediaOps is an admin console. It can show user watch history, IP addresses, devices, request history, library paths, file paths, and service API keys.

`SECRET_KEY` signs browser sessions. It must be a long random value and should live in the container environment, not in screenshots or public compose files. Changing it logs everyone out, which is useful if it ever leaks.

`/config` is persistent app state. If you save service details through the web UI, `/config/config.env` contains Plex, Seerr, Radarr, Sonarr, SABnzbd, and Tautulli credentials. Keep that volume private, back it up carefully, and do not expose it through file browsers or shared mounts.

The PostgreSQL database contains operational history: users, IPs, devices, sessions, requests, and library metadata. Treat database dumps as private household/server records.

Use HTTPS when MediaOps is reachable outside localhost or a trusted LAN. Set `BASE_URL` to the same public URL you use in the browser so Plex sign-in and MediaOps' origin checks agree. If a token or config file is accidentally committed or shared, rotate the affected Plex/app tokens rather than trying to hide the old value.

The Docker image runs as a non-root user.

Advanced optional endpoints:

- Plex webhooks are implemented but not required. They are disabled unless `PLEX_WEBHOOK_TOKEN` is set. Normal history collection uses Plex polling, not webhooks.
- API bearer tokens are only for automation against the JSON API. Normal browser use does not need them. Leave `API_ADMIN_TOKEN` and `API_TOKENS` blank unless you are deliberately wiring another tool into MediaOps.
- Home Assistant can poll `GET /api/integrations/homeassistant/status` with a scoped integration token issued from MediaOps Settings. MediaOps can also send request-change events to a Home Assistant webhook when `HOMEASSISTANT_WEBHOOK_URL` is set.
- AI tools can call the MCP-compatible JSON-RPC endpoint at `POST /api/mcp`. Give those clients a read-only token with `mcp.read` or `integrations.read`.

For Home Assistant, create the token in `Settings -> Integrations`. For environment-managed automation, tokens still need to start with `mo_`:

```sh
printf 'mo_%s\n' "$(openssl rand -hex 32)"
```

## Home Assistant

There are two supported paths.

**Polling integration:** copy `integrations/homeassistant/custom_components/mediaops` into Home Assistant's `custom_components/mediaops`, restart Home Assistant, then add the MediaOps integration from Devices & Services. It asks for:

- MediaOps URL, for example `https://mediaops.example.com`
- A token created in `Settings -> Integrations`

It creates sensors for live streams, active streams, paused streams, playback transcodes, background transcodes, active operations, pending requests, and current bandwidth.

**Webhook notifications:** create a Home Assistant automation with a webhook trigger, then set `HOMEASSISTANT_WEBHOOK_URL` in MediaOps Settings or environment. MediaOps posts events such as `requests_changed`, `request_approved`, `request_declined`, and `test`.

## AI / MCP

MediaOps exposes a small HTTP JSON-RPC endpoint at `POST /api/mcp` for AI tools that can speak MCP-style tool calls over HTTP. It uses the same bearer tokens as the JSON API.

Available tools:

- `mediaops.status`: current streams, operations, request counts, and bandwidth
- `mediaops.overview`: usage summary for 1, 7, 30, 90, or 365 days
- `mediaops.pending_requests`: recent requests waiting for approval
- `mediaops.history_search`: recent watch history by title or username

## Tautulli History Import

Tautulli is only needed if you want old watch history from before MediaOps was installed.

The normal path is the API:

1. Add `TAUTULLI_URL` and `TAUTULLI_API_KEY` in Settings.
2. Save or test the connection.
3. MediaOps starts importing Tautulli history and backfilling bandwidth estimates in the background.
4. After that, MediaOps keeps new history current from Plex live polling.

You can remove the Tautulli connection after the import if you do not want ongoing manual resyncs. Keep it configured if you want to use the Settings buttons later for `Sync new history`, `Full history rescan`, or `Backfill bandwidth`.

The mounted database import values are only for old/manual bootstrap workflows:

Leave these blank for a clean install:

```env
IMPORT_TAUTULLI_DB=
IMPORT_SEERR_DB=
IMPORT_RADARR_DB=
IMPORT_SONARR_DB=
```

These values are optional fallback paths for mounted legacy database files. They are not required for the Tautulli API import.

## Updating

For the standard compose install:

```sh
docker compose pull app
docker compose up -d app
```

The app runs database migrations on startup.

## Troubleshooting

`SECRET_KEY must be set` means the app refused to start with an empty or placeholder session secret. Generate one with `openssl rand -hex 32`.

`Cross-origin unsafe request blocked` usually means `BASE_URL` does not match the URL you are using in the browser, or your reverse proxy is not forwarding `Host` and `X-Forwarded-Proto` correctly.

If Plex sign-in works but setup cannot find the right server, use the custom Plex address option during setup. This is useful when Docker needs a LAN, container, or reverse-proxy URL different from the one Plex returns.

If requests show incomplete size or release data, connect Radarr and Sonarr and run the request sync job. Seerr alone knows the request; Arr knows the media, releases, files, and queue.
