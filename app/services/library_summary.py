"""Small cached Arr inventory summary; no playback-history scan or media mutations."""
import asyncio
from datetime import datetime, timedelta
from . import library_audit as audit
from .libraries import arr_clients, movie_size, series_size

_lock = asyncio.Lock()

async def summary(db, values=None):
    cached = audit.state(db, 'inventory-summary')
    if cached and datetime.utcnow() - datetime.fromisoformat(cached['checked_at']) < timedelta(minutes=10):
        return cached
    async with _lock:
        cached = audit.state(db, 'inventory-summary')
        if cached and datetime.utcnow() - datetime.fromisoformat(cached['checked_at']) < timedelta(minutes=10):
            return cached
        instances = [(kind, inst) for kind in ('radarr','sonarr') for inst in arr_clients(kind, values)]
        results = await asyncio.gather(*(audit.read(i['client'], '/api/v3/movie' if k=='radarr' else '/api/v3/series') for k,i in instances), return_exceptions=True)
        sources, errors, movie_ids, show_ids = [], [], set(), set()
        for (kind, inst), result in zip(instances, results):
            if isinstance(result, Exception):
                errors.append(dict(source=inst['name'], error=audit.error_label(result))); continue
            row = dict(source=inst['name'], kind=kind, bytes=0, movies=0, shows=0, episodes=0, managed=len(result), monitored=0, missing_movies=0)
            for item in result:
                row['monitored'] += bool(item.get('monitored'))
                if kind == 'radarr':
                    size = movie_size(item) or int((item.get('movieFile') or {}).get('size') or 0)
                    available = bool(item.get('hasFile') or item.get('movieFile'))
                    row['movies'] += available; row['missing_movies'] += not available
                    if available: movie_ids.add(('tmdb', item['tmdbId']) if item.get('tmdbId') else (inst['name'], item['id']))
                else:
                    size = series_size(item); stats = item.get('statistics') or {}
                    row['episodes'] += int(stats.get('episodeFileCount') or 0)
                    available = bool(stats.get('episodeFileCount'))
                    row['shows'] += available
                    if available: show_ids.add(('tvdb', item['tvdbId']) if item.get('tvdbId') else (inst['name'], item['id']))
                row['bytes'] += size
            sources.append(row)
        report = dict(checked_at=datetime.utcnow().isoformat(), sources=sources, errors=errors, complete=not errors,
            total_bytes=sum(s['bytes'] for s in sources), movies=len(movie_ids), shows=len(show_ids),
            episodes=sum(s['episodes'] for s in sources), movie_files=sum(s['movies'] for s in sources),
            monitored=sum(s['monitored'] for s in sources), managed=sum(s['managed'] for s in sources),
            missing_movies=sum(s['missing_movies'] for s in sources))
        # A failed refresh must not replace known library totals with zero.
        if errors and cached:
            return dict(cached, errors=errors, complete=False, refresh_failed=True)
        audit.save(db, 'inventory-summary', report)
        return report
