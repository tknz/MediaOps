"""Plex file-size review, independent of Arr ownership and deployment paths."""
from datetime import datetime
import hashlib
from urllib.parse import urlencode
from . import library_audit as audit, replacement_tracking as tracking
from .libraries import plex_catalogue, normalized_title, format_bytes, enrich_inventory_usage


def file_key(path): return hashlib.sha256(str(path).encode()).hexdigest()[:24]

def plex_owner(node, kind):
    ids = {g.get('id', '').split('://', 1)[0]:g.get('id', '').split('://', 1)[-1] for g in node.findall('Guid')}
    def ident(provider):
        try:return int(ids.get(provider, ''))
        except ValueError:return None
    return dict(kind=kind, guid=node.get('guid') or node.get('ratingKey'), title=node.get('title') or '',
        year=int(node.get('year') or 0) or None, tmdb_id=ident('tmdb'), tvdb_id=ident('tvdb'),
        rating_keys=[node.get('ratingKey')] if node.get('ratingKey') else [], last_viewed_at=int(node.get('lastViewedAt') or 0))

def plex_file(node, media, part, library, owner):
    duration=int(media.get('duration') or node.get('duration') or 0)/1000
    size=int(part.get('size') or 0)
    return dict(path=part.get('file'), bytes=size, title=owner['title'], year=owner.get('year'),
        kind=owner['kind'], owner_guid=owner['guid'], tmdb_id=owner.get('tmdb_id'), tvdb_id=owner.get('tvdb_id'),
        rating_key=node.get('ratingKey'), media_id=media.get('id'), part_id=part.get('id'),
        part_count=len(media.findall('Part')), sources=[library['title']], codec=media.get('videoCodec') or media.get('audioCodec') or 'Unknown',
        resolution=media.get('videoResolution') or 'Unknown', audio_codec=media.get('audioCodec') or 'Unknown',
        audio_channels=media.get('audioChannels'), duration_seconds=duration or None,
        bitrate_mbps=round(size*8/duration/1e6,2) if duration else None,
        edition=node.get('editionTitle') or '', episode=node.get('title') if owner['kind']=='show' else None,
        last_viewed_at=int(node.get('lastViewedAt') or 0))

def file_rows(db, inventory, source='all'):
    catalogue=plex_catalogue(db);files=catalogue.get('files',[]);owners={};jobs=tracking.tasks(db)
    watch=[dict(item) for item in inventory]
    for item in watch:
        identity=item.get('tvdb_id') if item['kind']=='series' else item.get('tmdb_id')
        if identity:owners.setdefault(('show' if item['kind']=='series' else 'movie',identity),[]).append(item)
    # Include unmanaged Plex identities in the same full-history pass. Keep remakes
    # in the matching context, even when the caller filters to one title or page.
    synthetic={};file_watch={}
    for f in files:
        if f['kind'] not in ('movie','show'): continue
        external=f.get('tvdb_id') if f['kind']=='show' else f.get('tmdb_id')
        matches=owners.get((f['kind'],external),[]) if external else []
        if not matches:
            key=(f['kind'],external or f.get('owner_guid') or (f['title'],f.get('year')))
            if key not in synthetic:
                item=dict(kind='series' if f['kind']=='show' else 'movie',id=-len(synthetic)-1,title=f['title'],year=f.get('year'),
                          source='Plex',source_index=-1,tmdb_id=f.get('tmdb_id'),tvdb_id=f.get('tvdb_id'),aliases=[])
                synthetic[key]=item;watch.append(item)
            matches=[synthetic[key]]
        file_watch[f['path']]=matches
    enrich_inventory_usage(db,watch)
    rows=[]
    for f in files:
        identity=f.get('tvdb_id') if f['kind']=='show' else f.get('tmdb_id')
        matches=owners.get((f['kind'],identity),[]) if identity else []
        row=dict(f,key=file_key(f['path']),managed=bool(matches),owners=[dict(source=m['source'],id=m['id'],monitored=m['monitored']) for m in matches])
        usage=file_watch.get(f['path'],[])
        latest=max((m for m in usage if m.get('last_watched_at')),key=lambda m:m['last_watched_at'],default={})
        row.update(plays=max((m.get('plays',0) for m in usage),default=0),watch_hours=max((m.get('watch_hours',0) for m in usage),default=0),
                   last_watched_at=latest.get('last_watched_at'),last_watched_by=latest.get('last_watched_by'),
                   active_viewers=sorted({v for m in usage for v in m.get('active_viewers',[])}),
                   playback_scope='series' if f['kind']=='show' else 'title',
                   history_status='ambiguous' if any(m.get('history_status')=='ambiguous' for m in usage) else 'recorded' if latest else 'unknown')
        plex_last=datetime.utcfromtimestamp(f['last_viewed_at']) if f.get('last_viewed_at') else None
        if plex_last and (not row['last_watched_at'] or plex_last>row['last_watched_at']):
            row.update(last_watched_at=plex_last,last_watched_by='Plex (viewer unavailable)')
            if row['history_status']!='ambiguous':row['history_status']='recorded'
        row['delete_url']='/libraries/space/files/'+row['key']+'/delete' if str(f.get('rating_key','')).isdigit() and f['kind'] in ('movie','show','video','artist') else None
        if matches:
            job=next((t for t in jobs if any(t['source']==m['source'] and t['movie_id']==m['id'] for m in matches)),None) if f['kind']=='movie' else None
            if job:row['operation']=job
            row['replacement_url']='/libraries/replacements?'+urlencode(dict(q=f['title'],source=source,min_gb=0))
            row['manage_url']='/libraries?'+urlencode(dict(q=matches[0]['title'],source=matches[0]['source']))
        rows.append(row)
    return rows


def page(db, inventory, *, q='', source='all', kind='all', managed='all', min_gb=25, sort='size', direction='desc', page=1, page_size=50):
    if not 0 <= min_gb <= 100000:raise ValueError('Minimum size must be between 0 and 100000 GB.')
    catalogue=plex_catalogue(db);files=catalogue.get('files',[]);rows=[]
    for row in file_rows(db,inventory,source):
        f=row;matches=row['owners']
        if q and normalized_title(q) not in normalized_title(f['title']+' '+f['path']):continue
        if source!='all' and source not in f['sources'] and source not in [m['source'] for m in matches]:continue
        if kind!='all' and f['kind']!=kind:continue
        if managed=='managed' and not matches or managed=='unmanaged' and matches:continue
        if f['bytes']<=min_gb*1e9:continue  # Strictly over the selected threshold.
        rows.append(row)
    sort=sort if sort in ('size','title','last_watched') else 'size';direction=direction if direction in ('asc','desc') else 'desc'
    rows.sort(key=lambda r:(normalized_title(r['title']),r['path']))
    field={'size':'bytes','title':'title','last_watched':'last_watched_at'}[sort]
    known=[r for r in rows if r.get(field) is not None];unknown=[r for r in rows if r.get(field) is None]
    known.sort(key=lambda r:normalized_title(r[field]) if field=='title' else r[field],reverse=direction=='desc');rows=known+unknown
    page_size=max(1,min(200,page_size));pages=max(1,(len(rows)+page_size-1)//page_size);page=max(1,min(pages,page))
    filters=dict(q=q,source=source,kind=kind,managed=managed,min_gb=min_gb,sort=sort,direction=direction,page_size=page_size)
    def url(n):return '/libraries/space?'+urlencode(dict(filters,page=n))
    return dict(items=rows[(page-1)*page_size:page*page_size],total_count=len(rows),total_bytes=sum(r['bytes'] for r in rows),
        unmanaged_count=sum(not r['managed'] for r in rows),page=page,pages=pages,filters=filters,checked_at=catalogue.get('checked_at'),
        complete=catalogue.get('complete',False),errors=catalogue.get('errors',[]),sources=sorted({s for f in files for s in f['sources']}|{i['source'] for i in inventory}),
        replacement_url='/libraries/replacements?'+urlencode(dict(q=q,source=source,min_gb=min_gb,managed=managed)),
        previous_url=url(page-1) if page>1 else None,next_url=url(page+1) if page<pages else None)
