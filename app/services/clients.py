from dataclasses import dataclass
import httpx
import xml.etree.ElementTree as ET


@dataclass
class ServiceConfig:
    url: str
    api_key: str | None = None
    token: str | None = None


class JsonServiceClient:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg

    async def get(self, path: str, **params):
        headers = {}
        if self.cfg.api_key:
            headers['X-Api-Key'] = self.cfg.api_key
        if self.cfg.token:
            headers['X-Plex-Token'] = self.cfg.token
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.cfg.url.rstrip('/')}/{path.lstrip('/')}", headers=headers, params=params)
            response.raise_for_status()
            ctype = response.headers.get('content-type','')
            return response.json() if 'json' in ctype else response.text

    def _headers(self):
        headers = {}
        if self.cfg.api_key:
            headers['X-Api-Key'] = self.cfg.api_key
        if self.cfg.token:
            headers['X-Plex-Token'] = self.cfg.token
        return headers

    async def post(self, path: str, **params):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.cfg.url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers(), params=params)
            response.raise_for_status()
            ctype = response.headers.get('content-type','')
            return response.json() if 'json' in ctype else response.text

    async def post_json(self, path: str, payload: dict):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.cfg.url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers(), json=payload)
            response.raise_for_status()
            ctype = response.headers.get('content-type','')
            return response.json() if 'json' in ctype else response.text

    async def put_json(self, path: str, payload: dict):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(f"{self.cfg.url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers(), json=payload)
            response.raise_for_status()
            ctype = response.headers.get('content-type','')
            return response.json() if 'json' in ctype else response.text

    async def delete(self, path: str, **params):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(f"{self.cfg.url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers(), params=params)
            response.raise_for_status()
            ctype = response.headers.get('content-type','')
            return response.json() if 'json' in ctype else response.text


class PlexClient(JsonServiceClient):
    async def libraries(self):
        xml = await self.get('/library/sections')
        root = ET.fromstring(xml)
        libs = []
        for item in root.findall('Directory'):
            loc = item.find('Location')
            libs.append({
                'key': item.attrib.get('key'), 'title': item.attrib.get('title'), 'type': item.attrib.get('type'),
                'uuid': item.attrib.get('uuid'), 'path': loc.attrib.get('path') if loc is not None else None,
                'scanned_at': item.attrib.get('scannedAt'),
            })
        return libs

    async def library_count(self, key: str):
        xml = await self.get(f'/library/sections/{key}/all', **{'X-Plex-Container-Start': 0, 'X-Plex-Container-Size': 0})
        return int(ET.fromstring(xml).attrib.get('totalSize', '0'))

    async def sessions(self):
        xml = await self.get('/status/sessions')
        root = ET.fromstring(xml)
        sessions = []
        for item in root:
            user = item.find('User')
            player = item.find('Player')
            session = item.find('Session')
            transcode = item.find('TranscodeSession')
            media = item.find('Media')
            part = media.find('Part') if media is not None else None
            video_stream = part.find("Stream[@streamType='1']") if part is not None else None
            audio_stream = part.find("Stream[@streamType='2']") if part is not None else None
            sessions.append({
                'session_key': item.attrib.get('sessionKey'),
                'rating_key': item.attrib.get('ratingKey'),
                'title': item.attrib.get('title'),
                'display_title': ((item.attrib.get('grandparentTitle') + ' - ') if item.attrib.get('grandparentTitle') else '') + (item.attrib.get('title') or ''),
                'grandparent_title': item.attrib.get('grandparentTitle'),
                'parent_title': item.attrib.get('parentTitle'),
                'media_index': int(item.attrib.get('index', '0')) if item.attrib.get('index') else None,
                'parent_media_index': int(item.attrib.get('parentIndex', '0')) if item.attrib.get('parentIndex') else None,
                'type': item.attrib.get('type'),
                'year': item.attrib.get('year'),
                'summary': item.attrib.get('summary'),
                'studio': item.attrib.get('studio'),
                'library': item.attrib.get('librarySectionTitle'),
                'thumb': item.attrib.get('thumb'),
                'art': item.attrib.get('art'),
                'view_offset': int(item.attrib.get('viewOffset', '0')),
                'duration': int(item.attrib.get('duration', '0')),
                'user': user.attrib.get('title') if user is not None else 'unknown',
                'user_id': user.attrib.get('id') if user is not None else None,
                'user_thumb': user.attrib.get('thumb') if user is not None else None,
                'player': player.attrib.get('title') if player is not None else None,
                'player_address': player.attrib.get('address') if player is not None else None,
                'remote_public_address': player.attrib.get('remotePublicAddress') if player is not None else None,
                'device': player.attrib.get('device') if player is not None else None,
                'machine_identifier': player.attrib.get('machineIdentifier') if player is not None else None,
                'model': player.attrib.get('model') if player is not None else None,
                'platform': player.attrib.get('platform') if player is not None else None,
                'platform_version': player.attrib.get('platformVersion') if player is not None else None,
                'product': player.attrib.get('product') if player is not None else None,
                'version': player.attrib.get('version') if player is not None else None,
                'state': player.attrib.get('state') if player is not None else None,
                'local': player.attrib.get('local') if player is not None else None,
                'secure': player.attrib.get('secure') if player is not None else None,
                'relayed': player.attrib.get('relayed') if player is not None else None,
                'session_id': session.attrib.get('id') if session is not None else None,
                'bandwidth': int(session.attrib.get('bandwidth', '0')) if session is not None else None,
                'location': session.attrib.get('location') if session is not None else None,
                'container': media.attrib.get('container') if media is not None else None,
                'bitrate': media.attrib.get('bitrate') if media is not None else None,
                'resolution': media.attrib.get('videoResolution') if media is not None else None,
                'video_codec': media.attrib.get('videoCodec') if media is not None else None,
                'audio_codec': media.attrib.get('audioCodec') if media is not None else None,
                'audio_channels': media.attrib.get('audioChannels') if media is not None else None,
                'file': part.attrib.get('file') if part is not None else None,
                'file_size': int(part.attrib.get('size', '0')) if part is not None else None,
                'part_decision': part.attrib.get('decision') if part is not None else None,
                'video_stream_title': video_stream.attrib.get('extendedDisplayTitle') if video_stream is not None else None,
                'audio_stream_title': audio_stream.attrib.get('extendedDisplayTitle') if audio_stream is not None else None,
                'transcode_decision': transcode.attrib.get('videoDecision') if transcode is not None else 'directplay',
                'transcode': dict(transcode.attrib) if transcode is not None else None,
            })
        return sessions

    async def terminate_session(self, session_id: str, reason: str | None = None):
        params = {'sessionId': session_id}
        if reason:
            params['reason'] = reason
        return await self.get('/status/sessions/terminate', **params)


class SeerrClient(JsonServiceClient):
    async def requests(self, take: int = 100, skip: int = 0):
        return await self.get('/api/v1/request', take=take, skip=skip, sort='added')

    async def users(self, take: int = 1000, skip: int = 0):
        return await self.get('/api/v1/user', take=take, skip=skip)

    async def user(self, user_id: int):
        return await self.get(f'/api/v1/user/{user_id}')

    async def user_requests(self, user_id: int, take: int = 100, skip: int = 0):
        return await self.get(f'/api/v1/user/{user_id}/requests', take=take, skip=skip)

    async def user_quota(self, user_id: int):
        return await self.get(f'/api/v1/user/{user_id}/quota')

    async def user_permissions(self, user_id: int):
        return await self.get(f'/api/v1/user/{user_id}/settings/permissions')

    async def update_user_permissions(self, user_id: int, permissions: int):
        return await self.post_json(f'/api/v1/user/{user_id}/settings/permissions', {'permissions': permissions})

    async def update_user(self, user_id: int, payload: dict):
        return await self.put_json(f'/api/v1/user/{user_id}', payload)

    async def approve_request(self, request_id: str):
        return await self.post(f'/api/v1/request/{request_id}/approve')

    async def decline_request(self, request_id: str):
        return await self.post(f'/api/v1/request/{request_id}/decline')

    async def delete_request(self, request_id: str):
        return await self.delete(f'/api/v1/request/{request_id}')


class RadarrClient(JsonServiceClient):
    async def movies(self):
        return await self.get('/api/v3/movie')

    async def set_movie_monitored(self, movie_id: int, monitored: bool):
        movie = await self.get(f'/api/v3/movie/{movie_id}')
        movie['monitored'] = monitored
        return await self.put_json(f'/api/v3/movie/{movie_id}', movie)

    async def delete_movie(self, movie_id: int, delete_files: bool = True):
        return await self.delete(f'/api/v3/movie/{movie_id}', deleteFiles=str(delete_files).lower(), addImportExclusion='false')

    async def queue(self):
        return await self.get('/api/v3/queue', page=1, pageSize=200, sortKey='timeleft', sortDirection='ascending', includeUnknownMovieItems='true')

    async def search_movie(self, title: str):
        rows = await self.movies()
        needle = title.lower().strip()
        return next((m for m in rows if (m.get('title') or '').lower().strip() == needle), None)

    async def trigger_search(self, movie_id: int):
        return await self.post_json('/api/v3/command', {'name': 'MoviesSearch', 'movieIds': [movie_id]})


class SonarrClient(JsonServiceClient):
    async def series(self):
        return await self.get('/api/v3/series')

    async def set_series_monitored(self, series_id: int, monitored: bool):
        series = await self.get(f'/api/v3/series/{series_id}')
        series['monitored'] = monitored
        return await self.put_json(f'/api/v3/series/{series_id}', series)

    async def delete_series(self, series_id: int, delete_files: bool = True):
        return await self.delete(f'/api/v3/series/{series_id}', deleteFiles=str(delete_files).lower(), addImportListExclusion='false')

    async def queue(self):
        return await self.get('/api/v3/queue', page=1, pageSize=200, sortKey='timeleft', sortDirection='ascending', includeUnknownSeriesItems='true')

    async def search_series(self, title: str):
        rows = await self.series()
        needle = title.lower().strip()
        return next((s for s in rows if (s.get('title') or '').lower().strip() == needle), None)

    async def trigger_search(self, series_id: int):
        return await self.post_json('/api/v3/command', {'name': 'SeriesSearch', 'seriesId': series_id})
