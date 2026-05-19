import sqlite3
from datetime import datetime
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import MediaRequest

STATUS = {1: 'pending', 2: 'approved', 3: 'declined', 4: 'available', 5: 'available'}


def _readonly_sqlite(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
    return sqlite3.connect(uri, uri=True)


def import_legacy_requests(db: Session, seerr_path: str, radarr_path: str, sonarr_path: str) -> int:
    if not all([seerr_path, radarr_path, sonarr_path]):
        return 0
    s = _readonly_sqlite(seerr_path); s.row_factory = sqlite3.Row
    r = _readonly_sqlite(radarr_path); r.row_factory = sqlite3.Row
    so = _readonly_sqlite(sonarr_path); so.row_factory = sqlite3.Row
    added = 0
    rows = s.execute('''
        select mr.id request_id, mr.type, mr.createdAt, mr.status,
               coalesce(nullif(u.username,''), nullif(u.plexUsername,''), u.email, 'unknown') requester,
               u.plexId plex_id, m.externalServiceId arr_id
        from media_request mr
        left join user u on u.id=mr.requestedById
        left join media m on m.id=mr.mediaId
    ''').fetchall()
    for row in rows:
        sid = str(row['request_id'])
        exists = db.scalar(select(MediaRequest.id).where(MediaRequest.source == 'legacy-seerr', MediaRequest.source_id == sid))
        if exists:
            continue
        title = 'Unknown'; seasons = None; size = None
        if row['type'] == 'movie':
            item = r.execute('''select mm.Title title, mf.Size size from Movies m left join MovieMetadata mm on mm.Id=m.MovieMetadataId left join MovieFiles mf on mf.Id=m.MovieFileId where m.Id=?''', (row['arr_id'],)).fetchone()
            if item:
                title, size = item['title'], item['size']
        else:
            series = so.execute('select Title from Series where Id=?', (row['arr_id'],)).fetchone()
            title = series['Title'] if series else 'Unknown'
            nums = [x[0] for x in s.execute('select seasonNumber from season_request where requestId=? order by seasonNumber', (row['request_id'],))]
            seasons = ','.join(map(str, nums)) or None
            if nums:
                placeholders = ','.join('?' * len(nums))
                size = so.execute(f'select sum(Size) from EpisodeFiles where SeriesId=? and SeasonNumber in ({placeholders})', (row['arr_id'], *nums)).fetchone()[0]
        db.add(MediaRequest(
            source='legacy-seerr', source_id=sid,
            requester_plex_id=str(row['plex_id']) if row['plex_id'] is not None else None,
            requester_name=row['requester'], request_type=row['type'], title=title,
            seasons=seasons, status=STATUS.get(row['status'], str(row['status'])),
            requested_at=datetime.fromisoformat(row['createdAt']), fulfilled_bytes=size,
        ))
        added += 1
    db.commit()
    return added
