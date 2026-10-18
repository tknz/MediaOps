"""Single catalogue for APScheduler registration, Jobs, API and MCP."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    id: str
    handler: str
    label: str
    description: str
    setting: str = ''
    default: int = 0
    unit: str = 'minutes'
    minimum: int = 1
    maximum: int = 1440
    delay: int = 1
    report_url: str = '/jobs'
    manual: bool = True


JOBS = (
    Job('plex-poll', 'scheduled_plex_poll', 'Plex live sessions', 'Playback, session endings and policies.', 'job_plex_live_seconds', 30, 'seconds', 10, 300, report_url='/live'),
    Job('downloads-poll', 'scheduled_downloads_poll', 'Download queue', 'Arr and SAB download status and speed.', 'job_downloads_seconds', 30, 'seconds', 10, 300, report_url='/live'),
    Job('plex-account-refresh', 'scheduled_plex_account_refresh', 'Plex accounts', 'Friends, shared access and watchlists.', 'job_plex_accounts_minutes', 60, 'minutes', 15, 1440, report_url='/users'),
    Job('request-sync', 'scheduled_request_sync', 'Request sync', 'Seerr requests and Arr availability.', 'job_requests_minutes', 15, 'minutes', 5, 1440, report_url='/requests'),
    Job('library-health', 'scheduled_library_health', 'Arr health checks', 'Health, lists, indexers and stalled imports; read-only.', 'job_library_health_minutes', 15, delay=60, report_url='/libraries/audit/report?view=health'),
    Job('library-storage', 'scheduled_library_storage', 'Duplicates & storage efficiency', 'Duplicate files, codecs and oversized media; read-only.', 'job_library_storage_hours', 168, 'hours', 1, 8760, 300, '/libraries/audit/report?view=duplicates'),
    Job('library-inventory', 'scheduled_library_inventory', 'Library inventory', 'Plex file sizes and content totals.', 'job_library_inventory_minutes', 10, report_url='/libraries/space'),
    Job('compact-replacements', 'scheduled_replacements', 'Find smaller replacements', 'Search releases for the largest movies. Downloads require approval.', 'job_replacements_hours', 168, 'hours', 1, 8760, 300, '/libraries/replacements'),
    Job('compact-replacement-tracking', 'scheduled_replacement_tracking', 'Track replacements', 'Match approved downloads to Radarr imports.', 'job_replacement_tracking_seconds', 30, 'seconds', 15, 300, 2, '/libraries/replacements/jobs'),
    Job('streaming-availability', 'scheduled_streaming_availability', 'Streaming availability', 'Refresh country-specific subscription listings in batches.', 'job_streaming_minutes', 1, delay=30, report_url='/libraries'),
    Job('compact-replacement-approval', 'scheduled_replacement_approval', 'Approved replacement submissions', 'Explicitly approved selections only.', report_url='/libraries/replacements/jobs', manual=False),
    Job('tautulli-history', 'tautulli_initial_sync_job', 'Tautulli history import', 'Connection bootstrap; imports history and bandwidth.', report_url='/settings#jobs', manual=False),
)
BY_ID = {job.id: job for job in JOBS}
SCHEDULE_DEFAULTS = {job.setting: str(job.default) for job in JOBS if job.setting}


def interval(job, values):
    try: value = int(values.get(job.setting, job.default))
    except (TypeError, ValueError): value = job.default
    return max(job.minimum, min(job.maximum, value))
