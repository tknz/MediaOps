"""Country-specific subscription listings from Seerr's TMDB/JustWatch metadata."""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse
from . import library_audit as audit
from .clients import SeerrClient, ServiceConfig
from .settings_store import all_settings

KEY = 'streaming-availability'
lock = asyncio.Lock()
TTL = timedelta(hours=24)


def region(values=None):
    value = str((values if values is not None else all_settings()).get('streaming_region') or 'NZ').upper()
    if len(value) != 2 or not value.isascii() or not value.isalpha():
        raise ValueError('Streaming country must be a two-letter country code.')
    return value


def identity(item, owners=()):
    kind = 'tv' if item['kind'] == 'series' else 'movie'
    external = item.get('tmdb_id')
    if not external and kind == 'tv' and item.get('tvdb_id'):
        matches = {o['tmdb_id'] for o in owners if o.get('kind') == 'show' and
                   o.get('tvdb_id') == item['tvdb_id'] and o.get('tmdb_id')}
        if len(matches) == 1: external = matches.pop()
    return f'{kind}:{int(external)}' if external else None


def fresh(row, now=None):
    try: return (now or datetime.utcnow()) - datetime.fromisoformat(row['checked_at']) < TTL
    except (KeyError, TypeError, ValueError): return False


def attach(db, inventory, values=None):
    country = region(values)
    cached = audit.state(db, KEY + '-' + country) or {}
    from .libraries import plex_catalogue
    owners = plex_catalogue(db, owners_only=True).get('owners', [])
    for item in inventory:
        key = identity(item, owners)
        row = cached.get('items', {}).get(key, {})
        status = ('known' if fresh(row) else 'stale') if row.get('checked_at') else 'unknown'
        item['streaming'] = dict(region=country, status=status, providers=row.get('providers', []),
                                 checked_at=row.get('checked_at'), link=row.get('link'),
                                 identity=key, error=row.get('error'))
    return inventory


def parse(details, country):
    countries = details.get('watchProviders')
    if not isinstance(countries, list): raise ValueError('Watch-provider metadata unavailable')
    listing = next((r for r in countries if r.get('iso_3166_1') == country), {})
    # Subscription only: a purchase or rental listing does not mean it is included.
    providers = {int(p['id']): str(p['name']) for p in listing.get('flatrate', []) if p.get('id') and p.get('name')}
    link = listing.get('link')
    if link and (urlparse(link).scheme != 'https' or urlparse(link).hostname not in ('www.themoviedb.org', 'themoviedb.org')): link = None
    return dict(checked_at=datetime.utcnow().isoformat(), providers=[dict(id=k, name=v) for k,v in sorted(providers.items(), key=lambda p:p[1])], link=link)


async def refresh(db, inventory=None, values=None, limit=100, force=False):
    cfg = values if values is not None else all_settings()
    country = region(cfg)
    if not cfg.get('seerr_url') or not cfg.get('seerr_api_key'): return
    async with lock:
        if inventory is None:
            from .libraries import library_inventory
            inventory, _ = await library_inventory(db, cfg, include_usage=False)
        attach(db, inventory, cfg)
        cached = audit.state(db, KEY + '-' + country) or dict(items={})
        items = cached.setdefault('items', {})
        candidates = {i['streaming']['identity'] for i in inventory if i['streaming']['identity']}
        now = datetime.utcnow()
        def due(key):
            row = items.get(key, {})
            if force: return True
            if row.get('retry_at') and datetime.fromisoformat(row['retry_at']) > now: return False
            return not fresh(row, now)
        candidates = sorted((k for k in candidates if due(k)), key=lambda k:items.get(k, {}).get('checked_at', ''))
        if limit is not None: candidates = candidates[:limit]
        client = SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key']))
        for start in range(0, len(candidates), 4):
            batch = candidates[start:start+4]
            results = await asyncio.gather(*(client.get('/api/v1/' + key.replace(':', '/')) for key in batch), return_exceptions=True)
            for key, result in zip(batch, results):
                try:
                    if isinstance(result, Exception): raise result
                    if int(result.get('id') or 0) != int(key.split(':')[1]): raise ValueError('Metadata identity mismatch')
                    items[key] = parse(result, country)
                except Exception as exc:
                    # Preserve the last successful lookup, clearly stale after its TTL.
                    items[key] = dict(items.get(key, {}), error=audit.error_label(exc), retry_at=(now+timedelta(hours=1)).isoformat())
            cached.update(region=country, updated_at=datetime.utcnow().isoformat())
            audit.save(db, KEY + '-' + country, cached)
        attach(db, inventory, cfg)


def matches(item, provider):
    row = item.get('streaming', {})
    if provider == 'all': return True
    if provider == 'unknown': return row.get('status') != 'known'
    if provider == 'none': return row.get('status') == 'known' and not row.get('providers')
    return row.get('status') == 'known' and any(str(p['id']) == provider for p in row.get('providers', []))
