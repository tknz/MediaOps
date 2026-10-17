# Library cleanup and audits

Implementation base: clean archived tknz/MediaOps checkout at
7e5bac224879137a5c5fbffe5147f2ab530b562e. The reported live tag e19ad86
is an ancestor, with three intervening existing commits; this change is not a
release payload approval. Work lives in an isolated source snapshot under the
registered project, preserving the archived repository and config-wizard.

## TV cleanup

Open `/libraries?preset=tv-cleanup`. Defaults: TV, recorded playback strictly
before the UTC instant two calendar years ago, all viewers, largest bytes first.
Calendar month subtraction clamps leap/month-end dates. An explicit `before`
date means midnight UTC at the start of that date. Equality is excluded.

6/12/24 months, explicit date, title/path, Sonarr source, viewer, history status,
size/last-watched/title sort, direction, page and page size persist in query
parameters. Filter/sort precedes pagination. Full matching count and byte totals
are returned. Unknown dates sort last in either direction. Unknown history is
separate and opt-in; ambiguous identities never qualify as recorded stale media.

Both `/libraries` HTML and `/api/libraries` use the same payload service. HTML
contains a functional form/table before JavaScript runs. JS uses that initial
payload without an unnecessary second inventory fetch. Existing detail views,
admin-only actions and delete confirmations remain available through JavaScript.

All stored sessions are streamed in batches, including null-title records; there
is no 60,000-record cutoff. Matching prefers Plex catalogue keys linked uniquely
by title/year; new Plex polling retains grandparentRatingKey through session
finalization. Otherwise NFKC/case/whitespace-normalized exact series titles and
legacy `Show - Episode` titles are used. Colliding identities and conflicting
catalogue years are treated conservatively as ambiguous. Stable ID coverage
improves as the existing Plex catalogue is synced and new sessions arrive.
The Tautulli importer also preserves series keys when supplied and can enrich
existing records on an ordinary reimport. There is no automatic history deletion, completion inference or historical
identity backfill. A missing catalogue/year/ID can still prevent disambiguation:
these are recorded-history results, not proof of complete lifetime viewing.

Fresh active session records (seen within two minutes) visibly warn in ordinary
results and disable JS action buttons. Cleanup presets exclude them regardless
of the viewer filter. This is a cached warning, not a server-side action lock;
state can change between viewing and an existing destructive action.

## Scheduled audits

Admin report: `/libraries/audit/report`; JSON: `/api/libraries/audit`.
Admin POST `/libraries/audit/run` schedules a health or storage scan. Scans run
in the background; reload the report for completion. Locks prevent overlapping
scans of the same kind within one app process.

Health: every 15 minutes while the app scheduler is running. Checks every
configured adult/kids Sonarr/Radarr health endpoint and paginates entire queues.
Native health covers list/indexer/root/download-client warnings. It records
native warning categories without retaining arbitrary messages or secret URLs.
Queue progress is compared across runs; two hours without observed byte/status
progress produces a *possible stall*, not a definitive failure. Paused downloads
and post-processing can be stationary. Authentication and identity/quality import
warnings require review in Arr. SAB provider/disk telemetry is not polled directly.

One retry of transient GET failures is the only automatic repair behavior.
No service settings, imports, queues or files are mutated. Existing configured
Home Assistant webhook receives meaningful health-fingerprint changes and
recovery counts; ordinary unchanged runs stay quiet. The report always exposes
current results even without a webhook.

Storage: weekly while the scheduler runs. The first health/storage scans are due one/five minutes after startup, or can be requested manually.
Uses Radarr movie file metadata and Sonarr episode files in batches of four.
Partial failures are explicit. Reports persist in the app database; the original
Plex history remains untouched. Due times use the persisted last report across restarts. Intervals are fixed
defaults rather than user-configurable schedules.

Duplicates group repeated external TMDB/TVDB ownership across configured Arr
instances. A paginated Plex scan also finds movie/episode identities with multiple
distinct file paths, exposing editions and quality metadata. Repeated references
to the same absolute path in combined and kids libraries are collapsed. Plex
metadata matching can itself be incorrect, so these remain review candidates. Titles alone do not establish duplicate identity. No hashes, inode
identity or unmanaged filesystem scans are performed. Different editions,
quality versions and hardlinks require review; reclaimable bytes remain unknown.
A combined Plex view of the same storage does not create an Arr duplicate.

Efficiency covers every codec, grouped by codec/resolution/source. Large files
(20 GB+) and high total bitrate (8 Mbps up to 720 lines, 15 Mbps above 720 through
1080, 40 Mbps above 1080) are review heuristics, not proof of waste. Missing
metadata is surfaced. Duration is parsed from Arr MediaInfo.RunTime; total
bitrate is derived from whole-file bytes/duration. HDR, bit depth, audio, size
and source are visible. A selectable total-output Mbps budget gives per-file
storage scenarios, not quality-equivalent H.265 predictions. Sample encodes,
Tdarr integration, automatic transcoding and deletion are outside this change.
Reported file bytes can overcount physical usage for hardlinks; duplicate and
conversion scenarios are not added together as guaranteed savings.

MediaInfo field formats were checked against upstream sources:
- https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/EpisodeFiles/MediaInfoResource.cs
- https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/MovieFiles/MediaInfoResource.cs

## Validation

`DATABASE_URL=sqlite:// python -m unittest discover -s tests -v`

Tests cover all-viewer recency, owner-only scope, exact cutoff, unknown/ambiguous
history, structured keys, active playback, full totals beyond 250 inventory and
60,000 sessions, URL roundtrip, leap-year handling, source filtering, HTML/API
parity, admin restrictions, codec-neutral candidates, conservative duplicates,
stall/recovery and full queue pagination without writes.

`node --check app/static/libraries.js` and a synthetic JS rendering check cover
visible fields, controls, escaping and active-playback button disabling.

`scripts/validate-native.sh` builds once on native Linux amd64, runs the tests
inside the exact image with no network, then smokes health/assets in that image.
It records per-phase start/end/elapsed/exit and retains logs. No registry push,
Compose replacement or production data mounts are part of validation.
