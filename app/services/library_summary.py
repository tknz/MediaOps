"""Plex-first stored inventory, with cached totals and an explicit Arr fallback."""
import asyncio
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from . import library_audit as audit
from .clients import PlexClient, ServiceConfig
from .libraries import arr_clients, movie_size, series_size
from .settings_store import all_settings

_lock = asyncio.Lock()
SCHEMA_VERSION = 2

async def plex_items(client, library, media_type):
    start = 0
    while True:
        xml = await audit.read(client, f"/library/sections/{library['key']}/all", type=media_type, includeGuids=1,
            **{'X-Plex-Container-Start':start, 'X-Plex-Container-Size':500})
        root = ET.fromstring(xml); nodes = list(root)
        for node in nodes: yield node
        start += len(nodes); total = int(root.get('totalSize') or 0)
        if (total and start >= total) or (not total and len(nodes) < 500): break
        if not nodes: raise ValueError('Incomplete Plex pagination')


def identity(node, library):
    return node.get('guid') or (library['key'], node.get('ratingKey'))

async def plex_summary(values):
    client = PlexClient(ServiceConfig(url=values['plex_server_url'], token=values['plex_server_token']))
    libraries = await client.libraries()
    limit = asyncio.Semaphore(3)
    async def scan(library):
        async with limit:
            kind = library['type']; media_type = {'movie':1,'show':4,'artist':10,'photo':13}.get(kind)
            if media_type is None: return None
            personal = kind == 'movie' and str(library.get('agent') or '').endswith('.none')
            result = dict(source=library['title'], kind='video' if personal else kind, paths={}, movies=set(), shows=set(), tracks=set(), videos=set(), photos=set(), episode_paths=set(), errors=[])
            try:
                shows = {}
                if kind == 'show':
                    async for node in plex_items(client, library, 2):
                        shows[node.get('ratingKey')] = identity(node, library)
                async for node in plex_items(client, library, media_type):
                    parts = node.findall('./Media/Part')
                    paths = [p.get('file') for p in parts if p.get('file')]
                    if not paths:
                        result['errors'].append(dict(source=library['title'], error='Media without file metadata')); continue
                    for part in parts:
                        path = part.get('file'); size = part.get('size')
                        if not path or size is None:
                            result['errors'].append(dict(source=library['title'], error='Missing file size')); continue
                        # Exact paths collapse combined libraries; distinct versions still consume bytes.
                        result['paths'][path] = max(result['paths'].get(path,0),int(size))
                    if kind == 'movie': result['videos' if personal else 'movies'].add(identity(node,library))
                    elif kind == 'show':
                        result['shows'].add(node.get('grandparentGuid') or shows.get(node.get('grandparentRatingKey')) or (library['key'],node.get('grandparentRatingKey') or node.get('ratingKey')))
                        result['episode_paths'].update(paths)
                    elif kind == 'artist': result['tracks'].add(identity(node,library))
                    elif kind == 'photo': result['photos'].add(identity(node,library))
            except Exception as exc:
                result['errors'].append(dict(source=library['title'],error=audit.error_label(exc)))
            return result
    results = await asyncio.gather(*(scan(library) for library in libraries))
    paths, sources, errors = {}, [], []
    sets = {name:set() for name in ('movies','shows','tracks','videos','photos','episode_paths')}
    for result in results:
        if result is None: continue
        for path,size in result['paths'].items(): paths[path] = max(paths.get(path,0),size)
        for name in sets: sets[name].update(result[name])
        # Keep errors compact; count affected entries without returning media paths/titles.
        unique_errors = {(e['source'],e['error']) for e in result['errors']}
        errors.extend(dict(source=source,error=error) for source,error in sorted(unique_errors))
        counts = {name:len(result[name]) for name in sets}
        sources.append(dict(source=result['source'],kind=result['kind'],bytes=sum(result['paths'].values()),file_count=len(result['paths']),
            movies=counts['movies'],shows=counts['shows'],episodes=counts['episode_paths'],tracks=counts['tracks'],videos=counts['videos'],photos=counts['photos']))
    return dict(checked_at=datetime.utcnow().isoformat(),provider='Plex',sources=sources,errors=errors,complete=not errors,
        total_bytes=sum(paths.values()),file_count=len(paths),movies=len(sets['movies']),shows=len(sets['shows']),episodes=len(sets['episode_paths']),
        tracks=len(sets['tracks']),videos=len(sets['videos']),photos=len(sets['photos']))


async def arr_summary(values):
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
    report.update(provider='Arr',tracks=0,videos=0,photos=0)
    return report


def fresh(cached):
    return bool(cached and cached.get('schema_version') == SCHEMA_VERSION and datetime.utcnow()-datetime.fromisoformat(cached['checked_at']) < timedelta(minutes=10))

async def summary(db, values=None, *, refresh=False):
    cached = audit.state(db, 'inventory-summary')
    if not refresh and fresh(cached): return cached
    # The scheduler refreshes in the background; serving old totals keeps Overview responsive.
    if cached and not refresh:
        return dict(cached,provider=cached.get('provider','Arr'),refreshing=True)
    async with _lock:
        cached = audit.state(db, 'inventory-summary')
        if not refresh and fresh(cached): return cached
        cfg = values if values is not None else all_settings()
        plex_configured = bool(cfg.get('plex_server_url') and cfg.get('plex_server_token'))
        try:
            report = await plex_summary(cfg) if plex_configured else await arr_summary(cfg)
        except Exception as exc:
            if cached and cached.get('provider')=='Plex':
                report = dict(cached,errors=[dict(source='Plex',error=audit.error_label(exc))],complete=False,refresh_failed=True,has_complete_snapshot=cached.get('has_complete_snapshot') or cached.get('complete',False))
                audit.save(db,'inventory-summary',report)
                return report
            report = await arr_summary(cfg)
            report.update(fallback_reason='Plex unavailable',errors=report['errors']+[dict(source='Plex',error=audit.error_label(exc))])
        if not report['complete'] and cached and (cached.get('has_complete_snapshot') or cached.get('complete')) and cached.get('provider')==report['provider']:
            report = dict(cached,errors=report['errors'],complete=False,refresh_failed=True,has_complete_snapshot=True)
            audit.save(db,'inventory-summary',report)
            return report
        report['schema_version'] = SCHEMA_VERSION
        report['has_complete_snapshot'] = bool(report['complete'])
        if report['provider']=='Arr' and plex_configured: report.setdefault('fallback_reason','Plex unavailable')
        audit.save(db, 'inventory-summary', report)
        return report
