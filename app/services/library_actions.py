"""Reviewed native library deletion; no automatic cleanup or history deletion."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from itsdangerous import URLSafeTimedSerializer
from ..config import settings
from . import library_audit as audit, libraries, streaming_availability as streaming
from .clients import PlexClient, ServiceConfig
from .settings_store import all_settings

lock = asyncio.Lock()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def signer():
    return URLSafeTimedSerializer(settings.secret_key, salt='library-delete-review')


async def inspect(db, kind, source_index, item_id, values=None):
    inst = libraries._client_for_action(kind, source_index, values)
    client = inst['client']
    row = await client.get(f'/api/v3/{"series" if kind == "series" else "movie"}/{item_id}')
    if int(row.get('id') or 0) != item_id: raise ValueError('Media identity changed.')
    item = (libraries.normalise_series if kind == 'series' else libraries.normalise_movie)(row, inst['name'], source_index)
    files = await client.get('/api/v3/episodefile', seriesId=item_id) if kind == 'series' else ([row['movieFile']] if row.get('movieFile') else [])
    episodes = await client.get('/api/v3/episode', seriesId=item_id) if kind == 'series' else []
    if any(int(f.get('seriesId', item_id)) != item_id for f in files): raise ValueError('Unexpected episode file ownership.')
    snapshot = dict(kind=kind, source_index=source_index, item_id=item_id,
                    source_identity=digest(client.cfg.url), title=row['title'], path=row.get('path'),
                    external_id=row.get('tvdbId') if kind == 'series' else row.get('tmdbId'),
                    monitored=row.get('monitored'), monitor_new=row.get('monitorNewItems'), seasons=row.get('seasons', []),
                    files=[{k:f.get(k) for k in ('id','path','relativePath','size')} for f in sorted(files, key=lambda f:f['id'])],
                    episodes=[{k:e.get(k) for k in ('id','seasonNumber','episodeNumber','airDateUtc','hasFile','episodeFileId','monitored')} for e in sorted(episodes, key=lambda e:e['id'])])
    streaming.attach(db, [item], values)
    return dict(item=item, snapshot=snapshot, fingerprint=digest(snapshot), file_count=len(files),
                bytes=sum(int(f.get('size') or 0) for f in files), client=client)


def token(review):
    item = review['item']
    return signer().dumps(dict(kind=item['kind'], source_index=item['source_index'], item_id=item['id'], fingerprint=review['fingerprint']))


def public(review):
    return {k:v for k,v in review.items() if k not in ('client', 'snapshot')}


def future_episode(episode, now):
    if episode.get('hasFile') or episode.get('episodeFileId'): return False
    raw = episode.get('airDateUtc')
    if not raw: return True  # unaired or unscheduled; already stored episodes were excluded above
    aired = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return (aired.replace(tzinfo=timezone.utc) if aired.tzinfo is None else aired.astimezone(timezone.utc)) > now


async def playback_guard(db, review, values=None):
    cfg = values if values is not None else all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'): return
    plex = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
    root = ET.fromstring(await plex.get('/status/sessions'))
    item = review['item']
    media_kind = 'show' if item['kind'] == 'series' else 'movie'
    field = 'tvdb_id' if item['kind'] == 'series' else 'tmdb_id'
    rating_keys = {str(k) for o in libraries.plex_catalogue(db, owners_only=True).get('owners', [])
                   if o.get('kind') == media_kind and item.get(field) and o.get(field) == item[field]
                   for k in o.get('rating_keys', [])}
    for node in root:
        title = node.get('grandparentTitle') if item['kind'] == 'series' else node.get('title')
        rating = node.get('grandparentRatingKey') if item['kind'] == 'series' else node.get('ratingKey')
        if str(rating) in rating_keys or libraries.normalized_title(title) == libraries.normalized_title(item['title']):
            raise ValueError('This title is playing now. Wait until playback ends.')


async def execute(db, confirmation, mode, actor='admin', values=None):
    if mode not in ('remove', 'files_future'): raise ValueError('Choose a deletion mode.')
    data = signer().loads(confirmation, max_age=600)
    if mode == 'files_future' and data['kind'] != 'series': raise ValueError('Future episode monitoring is only available for TV.')
    async with lock:
        operation = 'library-delete-' + digest(confirmation)
        if audit.state(db, operation): raise ValueError('This deletion was already submitted. Check the source before retrying.')
        review = await inspect(db, data['kind'], data['source_index'], data['item_id'], values)
        if review['fingerprint'] != data['fingerprint']: raise ValueError('Files or monitoring changed. Review the deletion again.')
        await playback_guard(db, review, values)
        client = review['client']; item = review['item']; snap = review['snapshot']
        queue = await audit.full_queue(client)
        owner_field = 'seriesId' if item['kind'] == 'series' else 'movieId'
        if any(q.get(owner_field) == item['id'] for q in queue):
            raise ValueError('This title has download activity. Resolve it in the source before deleting.')
        if mode == 'files_future' and not snap['files']: raise ValueError('No episode files remain to delete.')
        # Persist the native identity, file list and monitoring manifest before any write.
        event = dict(status='submitted', actor=actor, mode=mode, at=datetime.utcnow().isoformat(), manifest=snap)
        audit.save(db, operation, event)
        try:
            if mode == 'remove':
                await libraries.delete_library_item(item['kind'], item['source_index'], item['id'], True, values)
            else:
                now = datetime.now(timezone.utc)
                future = [e['id'] for e in snap['episodes'] if future_episode(e, now) and (e.get('seasonNumber') != 0 or e.get('monitored'))]
                past = [e['id'] for e in snap['episodes'] if e['id'] not in set(future)]
                # Stop backfill before deleting, without an automatic search or global Sonarr setting change.
                await client.put_json('/api/v3/series/editor', dict(seriesIds=[item['id']], monitored=True, monitorNewItems='all'))
                series = await client.get(f"/api/v3/series/{item['id']}")
                for season in series.get('seasons', []):
                    if season['seasonNumber'] != 0: season['monitored'] = True
                await client.put_json(f"/api/v3/series/{item['id']}", series)
                for ids, monitored in ((past, False), (future, True)):
                    if ids: await client.put_json('/api/v3/episode/monitor', dict(episodeIds=ids, monitored=monitored))
                # Ensure settings persisted before the destructive operation.
                current = await client.get('/api/v3/episode', seriesId=item['id'])
                monitors = {e['id']:e.get('monitored') for e in current}
                current_series = await client.get(f"/api/v3/series/{item['id']}")
                if not current_series.get('monitored') or current_series.get('monitorNewItems') != 'all' or any(not s.get('monitored') for s in current_series.get('seasons', []) if s['seasonNumber'] != 0) or any(monitors.get(i) is not False for i in past) or any(monitors.get(i) is not True for i in future):
                    raise ValueError('Monitoring was not confirmed. Files were retained; review Sonarr.')
                await client.delete_json('/api/v3/episodefile/bulk', dict(episodeFileIds=[f['id'] for f in snap['files']]))
                remaining = await client.get('/api/v3/episodefile', seriesId=item['id'])
                if {f['id'] for f in remaining} & {f['id'] for f in snap['files']}:
                    raise ValueError('Some files remain. Review Sonarr before retrying.')
            audit.save(db, operation, dict(event, status='completed', completed_at=datetime.utcnow().isoformat()))
        except Exception:
            audit.save(db, operation, dict(event, status='needs_review', message='Operation may be partially complete. Check Sonarr/Radarr before retrying.'))
            raise
        return dict(ok=True, mode=mode, title=item['title'], removed_bytes=review['bytes'])
