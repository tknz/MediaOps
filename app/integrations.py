from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from .api_auth import AuthContext, require_auth
from .db import get_db
from .models import ActiveDownloadItem, ActivePlexSession, MediaRequest, PlexSession, UserBlock, UserPolicy
from .services.clients import PlexClient, ServiceConfig
from .services.network_enrichment import lookup_isp, reverse_dns
from .services.settings_store import all_settings, media_server_label
from .services.webhooks import notify_homeassistant


router = APIRouter()

STATUS_LABEL = {
    '1': 'requested',
    '2': 'approved',
    '3': 'declined',
    '4': 'available',
    '5': 'available',
    'pending': 'requested',
    'requested': 'requested',
    'approved': 'approved',
    'declined': 'declined',
    'available': 'available',
}


def _require_integration_read(context: AuthContext) -> None:
    if not (context.is_admin or context.has_scope('integrations.read') or context.has_scope('ha.read') or context.has_scope('mcp.read')):
        raise HTTPException(status_code=403, detail='Scope required: integrations.read')


def _require_integration_write(context: AuthContext) -> None:
    if not (context.is_admin or context.has_scope('integrations.write') or context.has_scope('ha.write')):
        raise HTTPException(status_code=403, detail='Scope required: integrations.write')


def _require_integration_admin(context: AuthContext) -> None:
    if not (context.is_admin or context.has_scope('integrations.admin') or context.has_scope('ha.admin')):
        raise HTTPException(status_code=403, detail='Scope required: integrations.admin')


def _status_slug(value: str | None) -> str:
    return STATUS_LABEL.get(str(value or '').lower(), str(value or 'unknown').lower())


def _active_download_payload(row: ActiveDownloadItem) -> dict:
    return {
        'key': row.item_key,
        'source': row.source,
        'title': row.title,
        'status': row.status or row.tracked_download_status or 'unknown',
        'quality': row.quality,
        'size_bytes': row.size_bytes,
        'size_left_bytes': row.size_left_bytes,
        'progress': row.progress,
        'message': row.message,
        'last_seen_at': row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _session_payload(row: ActivePlexSession) -> dict:
    return {
        'session_key': row.session_key,
        'session_id': row.session_id,
        'user': row.username,
        'user_url': f'/users/{row.username}',
        'title': row.grandparent_title or row.title or row.content_title,
        'subtitle': row.content_title if row.grandparent_title else row.parent_title,
        'media_type': row.media_type,
        'state': row.state,
        'library': row.library,
        'thumb': row.thumb_path,
        'player': row.player,
        'device': row.device,
        'machine_identifier': row.machine_id,
        'platform': row.platform,
        'player_address': row.player_address,
        'remote_public_address': row.remote_public_address,
        'ip_address': row.ip_address,
        'bandwidth_kbps': int(row.bandwidth_kbps or 0),
        'transcode_decision': row.transcode_decision,
        'started_at': row.started_at.isoformat() if row.started_at else None,
        'last_seen_at': row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _block_payload(row: UserBlock) -> dict:
    return {
        'id': f'block:{row.id}',
        'username': row.username,
        'type': row.block_type,
        'value': row.value,
        'label': row.label,
        'message': row.message,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


def _policy_ban_payload(row: UserPolicy) -> dict:
    return {
        'id': f'policy:{row.id}',
        'username': row.username,
        'type': 'user',
        'value': row.username,
        'label': row.username,
        'message': row.block_message,
        'created_at': None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


async def _enriched_session_payloads(rows: list[ActivePlexSession]) -> list[dict]:
    payloads = []
    for row in rows[:10]:
        payload = _session_payload(row)
        address = payload.get('remote_public_address') or payload.get('player_address') or payload.get('ip_address') or ''
        enrich = await lookup_isp(address) if address else {'isp': None, 'org': None, 'as': None}
        payloads.append({**payload, 'ptr': reverse_dns(address) if address else None, **enrich})
    return payloads


def homeassistant_status_payload(db: Session, session_payloads: list[dict] | None = None) -> dict:
    sessions = db.scalars(select(ActivePlexSession).order_by(ActivePlexSession.last_seen_at.desc())).all()
    operations = db.scalars(select(ActiveDownloadItem).order_by(ActiveDownloadItem.last_seen_at.desc(), ActiveDownloadItem.title)).all()
    blocks = db.scalars(select(UserBlock).where(UserBlock.active == True).order_by(UserBlock.updated_at.desc(), UserBlock.created_at.desc()).limit(50)).all()
    policy_bans = db.scalars(select(UserPolicy).where(UserPolicy.blocked == True).order_by(UserPolicy.updated_at.desc()).limit(50)).all()
    bans = [_policy_ban_payload(row) for row in policy_bans] + [_block_payload(row) for row in blocks]
    active_sessions = [s for s in sessions if (s.state or '').lower() != 'paused']
    active_ops = [o for o in operations if (o.status or '').lower() not in {'completed', 'complete'}]
    background_transcodes = [o for o in operations if (o.source or '').lower() == 'plex transcode']
    request_rows = db.execute(select(MediaRequest.status, func.count(MediaRequest.id)).group_by(MediaRequest.status)).all()
    requests = {'requested': 0, 'approved': 0, 'declined': 0, 'available': 0, 'unknown': 0}
    for status, count in request_rows:
        requests[_status_slug(status) if _status_slug(status) in requests else 'unknown'] += int(count or 0)
    total_kbps = sum(int(s.bandwidth_kbps or 0) for s in active_sessions)
    return {
        'ok': True,
        'app': 'MediaOps',
        'server': media_server_label(),
        'updated_at': datetime.utcnow().isoformat() + 'Z',
        'live_streams': len(sessions),
        'active_streams': len(active_sessions),
        'paused_streams': len(sessions) - len(active_sessions),
        'playback_transcodes': sum(1 for s in active_sessions if s.transcode_decision == 'transcode'),
        'background_transcodes': len(background_transcodes),
        'active_operations': len(active_ops),
        'active_downloads': len([o for o in active_ops if (o.source or '').lower() != 'plex transcode']),
        'bandwidth_kbps': total_kbps,
        'bandwidth_mbps': round(total_kbps / 1000, 2),
        'requests': requests,
        'pending_requests': requests['requested'],
        'sessions': session_payloads if session_payloads is not None else [_session_payload(row) for row in sessions[:10]],
        'operations': [_active_download_payload(row) for row in operations[:10]],
        'active_bans': len(bans),
        'bans': bans,
    }


def _overview_payload(db: Session, days: int = 30) -> dict:
    days = days if days in {1, 7, 30, 90, 365} else 30
    since = datetime.utcnow() - timedelta(days=days)
    row = db.execute(select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        func.count(func.distinct(PlexSession.username)),
    ).where(PlexSession.started_at >= since)).one()
    return {
        'period_days': days,
        'plays': int(row[0] or 0),
        'hours': round(float(row[1] or 0) / 3600, 1),
        'terabytes': round(float(row[2] or 0) / 1e12, 3),
        'transcodes': int(row[3] or 0),
        'users': int(row[4] or 0),
    }


@router.get('/api/integrations/homeassistant/status')
async def homeassistant_status(db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_read(context)
    sessions = db.scalars(select(ActivePlexSession).order_by(ActivePlexSession.last_seen_at.desc())).all()
    return homeassistant_status_payload(db, await _enriched_session_payloads(sessions))


@router.get('/api/integrations/homeassistant/sessions/{session_key}/art')
async def homeassistant_session_art(session_key: str, db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_read(context)
    row = db.get(ActivePlexSession, session_key)
    if not row or not row.thumb_path:
        return Response(status_code=404)
    cfg = all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return Response(status_code=404)
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{cfg['plex_server_url'].rstrip('/')}{row.thumb_path}", headers={'X-Plex-Token': cfg['plex_server_token']})
        ctype = resp.headers.get('content-type', '')
        if resp.status_code >= 400:
            return Response(status_code=resp.status_code)
        if not ctype.startswith('image/'):
            return Response(status_code=415)
        return Response(content=resp.content, media_type=ctype, status_code=resp.status_code)


@router.post('/api/integrations/homeassistant/sessions/{session_key}/terminate')
async def homeassistant_terminate_session(session_key: str, payload: dict = Body(default={}), db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_write(context)
    row = db.get(ActivePlexSession, session_key)
    if not row or not row.session_id:
        raise HTTPException(status_code=404, detail='Active session not found')
    cfg = all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        raise HTTPException(status_code=503, detail='Plex is not configured')
    reason = str((payload or {}).get('reason') or 'Stopped by MediaOps from Home Assistant')
    await PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token'])).terminate_session(row.session_id, reason)
    return {'ok': True, 'session_key': session_key, 'action': 'terminate'}


def _upsert_session_block(db: Session, row: ActivePlexSession, block_type: str, value: str, label: str, message: str) -> UserBlock:
    username = row.username or 'Unknown'
    block = db.scalar(select(UserBlock).where(
        func.lower(UserBlock.username) == username.lower(),
        UserBlock.block_type == block_type,
        UserBlock.value == value,
    ))
    if not block:
        block = UserBlock(username=username, block_type=block_type, value=value)
        db.add(block)
    block.label = label or block.label
    block.message = message or block.message
    block.active = True
    db.commit()
    db.refresh(block)
    return block


@router.post('/api/integrations/homeassistant/sessions/{session_key}/ban-ip')
async def homeassistant_ban_session_ip(session_key: str, payload: dict = Body(default={}), db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_admin(context)
    row = db.get(ActivePlexSession, session_key)
    if not row or not row.remote_public_address:
        raise HTTPException(status_code=404, detail='Session public IP not found')
    message = str((payload or {}).get('message') or 'The IP address you are connecting from is banned. Please contact your server administrator.')
    block = _upsert_session_block(db, row, 'ip', row.remote_public_address, row.remote_public_address, message)
    if row.session_id:
        cfg = all_settings()
        if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
            await PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token'])).terminate_session(row.session_id, message)
    return {'ok': True, 'session_key': session_key, 'block_id': block.id, 'action': 'ban-ip'}


@router.post('/api/integrations/homeassistant/sessions/{session_key}/ban-device')
async def homeassistant_ban_session_device(session_key: str, payload: dict = Body(default={}), db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_admin(context)
    row = db.get(ActivePlexSession, session_key)
    if not row or not row.machine_id:
        raise HTTPException(status_code=404, detail='Session device identifier not found')
    label = row.product or row.player or row.device or row.machine_id
    message = str((payload or {}).get('message') or 'The device you are connecting from is banned. Please contact your server administrator.')
    block = _upsert_session_block(db, row, 'device', row.machine_id, label, message)
    if row.session_id:
        cfg = all_settings()
        if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
            await PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token'])).terminate_session(row.session_id, message)
    return {'ok': True, 'session_key': session_key, 'block_id': block.id, 'action': 'ban-device'}


@router.post('/api/integrations/homeassistant/sessions/{session_key}/ban-user')
async def homeassistant_ban_session_user(session_key: str, payload: dict = Body(default={}), db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_admin(context)
    row = db.get(ActivePlexSession, session_key)
    if not row or not row.username:
        raise HTTPException(status_code=404, detail='Session user not found')
    message = str((payload or {}).get('message') or 'Your streaming access is currently banned. Please contact your server administrator.')
    policy = db.scalar(select(UserPolicy).where(func.lower(UserPolicy.username) == row.username.lower()))
    if not policy:
        policy = UserPolicy(username=row.username)
        db.add(policy)
    policy.blocked = True
    policy.block_message = message
    db.commit()
    if row.session_id:
        cfg = all_settings()
        if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
            await PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token'])).terminate_session(row.session_id, message)
    return {'ok': True, 'session_key': session_key, 'username': row.username, 'action': 'ban-user'}


@router.post('/api/integrations/homeassistant/bans/{ban_id}/unban')
async def homeassistant_unban(ban_id: str, db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_admin(context)
    kind, _, raw_id = ban_id.partition(':')
    if not raw_id and kind.isdigit():
        kind, raw_id = 'block', kind
    if kind == 'policy' and raw_id.isdigit():
        policy = db.get(UserPolicy, int(raw_id))
        if not policy or not policy.blocked:
            raise HTTPException(status_code=404, detail='Active user ban not found')
        policy.blocked = False
        policy.block_message = None
    elif kind == 'block' and raw_id.isdigit():
        block = db.get(UserBlock, int(raw_id))
        if not block or not block.active:
            raise HTTPException(status_code=404, detail='Active ban not found')
        block.active = False
    else:
        raise HTTPException(status_code=404, detail='Active ban not found')
    db.commit()
    return {'ok': True, 'ban_id': ban_id, 'action': 'unban'}


@router.post('/api/integrations/homeassistant/webhook/test')
async def homeassistant_webhook_test(db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_write(context)
    delivered = await notify_homeassistant('test', homeassistant_status_payload(db))
    return {'ok': delivered}


@router.get('/api/integrations')
def integrations_index(context: AuthContext = Depends(require_auth)):
    _require_integration_read(context)
    return {
        'homeassistant': {
            'status_url': '/api/integrations/homeassistant/status',
            'test_webhook_url': '/api/integrations/homeassistant/webhook/test',
        },
        'mcp': {'json_rpc_url': '/api/mcp'},
    }


def _mcp_tool_result(payload: Any) -> dict:
    return {'content': [{'type': 'text', 'text': payload if isinstance(payload, str) else _json_text(payload)}]}


def _json_text(payload: Any) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _mcp_tools() -> list[dict]:
    return [
        {
            'name': 'mediaops.status',
            'description': 'Current MediaOps live streams, active operations, request counts, and bandwidth.',
            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
        {
            'name': 'mediaops.overview',
            'description': 'Usage summary for a recent period.',
            'inputSchema': {'type': 'object', 'properties': {'days': {'type': 'integer', 'enum': [1, 7, 30, 90, 365]}}, 'additionalProperties': False},
        },
        {
            'name': 'mediaops.pending_requests',
            'description': 'Recent Seerr requests that still need approval.',
            'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, 'additionalProperties': False},
        },
        {
            'name': 'mediaops.history_search',
            'description': 'Search recent watch history by title or username.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'username': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                },
                'additionalProperties': False,
            },
        },
    ]


def _pending_requests(db: Session, limit: int = 20) -> list[dict]:
    rows = db.scalars(
        select(MediaRequest)
        .where(MediaRequest.status.in_(['pending', 'requested', '1']))
        .order_by(MediaRequest.requested_at.desc())
        .limit(max(1, min(limit, 50)))
    ).all()
    return [{
        'id': row.id,
        'source_id': row.source_id,
        'requester': row.requester_name,
        'type': row.request_type,
        'title': row.title,
        'seasons': row.seasons,
        'requested_at': row.requested_at.isoformat() if row.requested_at else None,
    } for row in rows]


def _history_search(db: Session, context: AuthContext, args: dict) -> list[dict]:
    query = str(args.get('query') or '').strip()
    username = str(args.get('username') or '').strip()
    limit = max(1, min(int(args.get('limit') or 20), 50))
    filters = []
    if query:
        needle = f'%{query.lower()}%'
        filters.append(or_(
            func.lower(PlexSession.title).like(needle),
            func.lower(PlexSession.grandparent_title).like(needle),
            func.lower(PlexSession.content_title).like(needle),
        ))
    if context.is_admin and username:
        filters.append(func.lower(PlexSession.username) == username.lower())
    elif not context.is_admin:
        filters.append(func.lower(PlexSession.username) == context.username.lower())
    rows = db.scalars(select(PlexSession).where(*filters).order_by(PlexSession.started_at.desc()).limit(limit)).all()
    return [{
        'username': row.username,
        'title': row.grandparent_title or row.title or row.content_title,
        'subtitle': row.content_title if row.grandparent_title else row.parent_title,
        'media_type': row.media_type,
        'watched_seconds': row.watched_seconds,
        'bytes_streamed': row.bytes_streamed,
        'transcode_decision': row.transcode_decision,
        'started_at': row.started_at.isoformat() if row.started_at else None,
    } for row in rows]


@router.post('/api/mcp')
async def mcp_json_rpc(payload: dict = Body(...), db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    _require_integration_read(context)
    method = payload.get('method')
    rpc_id = payload.get('id')
    params = payload.get('params') or {}
    try:
        if method == 'initialize':
            result = {
                'protocolVersion': '2024-11-05',
                'serverInfo': {'name': 'mediaops', 'version': '0.1-beta'},
                'capabilities': {'tools': {}},
            }
        elif method == 'tools/list':
            result = {'tools': _mcp_tools()}
        elif method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            if name == 'mediaops.status':
                result = _mcp_tool_result(homeassistant_status_payload(db))
            elif name == 'mediaops.overview':
                result = _mcp_tool_result(_overview_payload(db, int(args.get('days') or 30)))
            elif name == 'mediaops.pending_requests':
                result = _mcp_tool_result(_pending_requests(db, int(args.get('limit') or 20)))
            elif name == 'mediaops.history_search':
                result = _mcp_tool_result(_history_search(db, context, args))
            else:
                raise HTTPException(status_code=404, detail=f'Unknown tool: {name}')
        else:
            raise HTTPException(status_code=404, detail=f'Unknown MCP method: {method}')
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}
    except HTTPException:
        raise
    except Exception as exc:
        return {'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': -32603, 'message': str(exc)}}


@router.get('/api/mcp')
def mcp_info(context: AuthContext = Depends(require_auth)):
    _require_integration_read(context)
    return {'name': 'mediaops', 'transport': 'http-json-rpc', 'endpoint': '/api/mcp', 'tools': [tool['name'] for tool in _mcp_tools()]}
