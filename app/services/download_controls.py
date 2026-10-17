"""SAB queue telemetry and narrowly scoped, explicit per-job controls."""
from datetime import datetime
import re
import httpx
from . import library_audit as audit
from .settings_store import all_settings

async def sab_request(values=None, *, mutation=False, **params):
    cfg = values or all_settings()
    if not cfg.get('sabnzbd_url') or not cfg.get('sabnzbd_api_key'):
        raise ValueError('SABnzbd is not configured')
    # Credentials stay in the request body for writes and are never returned or logged.
    params.update(apikey=cfg['sabnzbd_api_key'], output='json')
    async with httpx.AsyncClient(timeout=15) as client:
        url = cfg['sabnzbd_url'].rstrip('/') + '/api'
        response = await client.post(url, data=params) if mutation else await client.get(url, params=params)
        response.raise_for_status(); result = response.json()
    if result.get('error') or result.get('status') is False: raise ValueError('SABnzbd rejected the request')
    return result

async def queue(values=None):
    slots, start, summary = [], 0, {}
    while True:
        q = (await sab_request(values, mode='queue', start=start, limit=100))['queue']
        if start == 0: summary = {k:q.get(k) for k in ('kbpersec','paused','timeleft')}
        batch = q.get('slots',[]); slots.extend(batch); start += len(batch)
        if start >= int(q.get('noofslots_total', q.get('noofslots',0))): break
        if not batch: raise ValueError('Incomplete SABnzbd queue')
    return summary, slots


def snapshot(summary, slots, previous=None, now=None):
    now = now or datetime.utcnow(); previous = previous or {}; items = {}
    elapsed = (now - datetime.fromisoformat(previous['checked_at'])).total_seconds() if previous.get('checked_at') else 0
    for slot in slots:
        key = slot['nzo_id']; left = int(float(slot.get('mbleft') or 0)*1024*1024)
        status = slot.get('status') or 'Unknown'; old = previous.get('items',{}).get(key)
        speed = max(0,int((old['size_left_bytes']-left)/elapsed)) if old and 5<=elapsed<=120 and status.lower()=='downloading' else None
        if status.lower() in ('paused','queued'): speed=0
        items[key] = dict(download_id=key, title=slot.get('filename') or 'Unknown', status=status,
            size_bytes=int(float(slot.get('mb') or 0)*1024*1024), size_left_bytes=left,
            progress=float(slot.get('percentage') or 0), timeleft=slot.get('timeleft'),
            category=slot.get('cat'), priority=slot.get('priority'), speed_bps=speed,
            controls=status.lower() in ('downloading','queued','paused','fetching','propagating'))
    return dict(checked_at=now.isoformat(), items=items, queue_speed_bps=int(float(summary.get('kbpersec') or 0)*1024), paused=bool(summary.get('paused')))


def attach(rows, report):
    if not report: return rows
    fresh = (datetime.utcnow()-datetime.fromisoformat(report['checked_at'])).total_seconds()<120
    matched = set()
    for row in rows:
        item = report.get('items',{}).get(row.get('download_id'))
        if not item: continue
        matched.add(row['download_id'])
        if fresh: row.update({k:v for k,v in item.items() if k!='title'})
        row['client'] = 'SABnzbd'; row['controls'] = bool(fresh and item.get('controls'))
    if fresh:
        for key,item in report.get('items',{}).items():
            if key not in matched:
                rows.append(dict(item,item_key='sab:'+key,source='SABnzbd',client='SABnzbd'))
    return rows

async def control(download_id, action, priority=None, values=None):
    if not re.fullmatch(r'SABnzbd_nzo_[A-Za-z0-9_-]+', download_id): raise ValueError('Invalid job identifier')
    if action not in ('pause','resume','priority'): raise ValueError('Unsupported control')
    if action=='priority' and priority not in ('-1','0','1'): raise ValueError('Invalid priority')
    _, slots = await queue(values)
    row = next((s for s in slots if s['nzo_id']==download_id),None)
    if not row or str(row.get('status','')).lower() not in ('downloading','queued','paused','fetching','propagating'):
        raise ValueError('Job is no longer controllable; refresh the queue')
    if action=='pause' and str(row['status']).lower()=='paused': raise ValueError('Job is already paused')
    if action=='resume' and str(row['status']).lower()!='paused': raise ValueError('Job is not paused')
    params = dict(mode='queue',name=action,value=download_id)
    if action=='priority': params['value2']=priority
    # Deliberately no retries: a timed-out mutation may already have succeeded.
    result = await sab_request(values, mutation=True, **params)
    if action=='priority':
        if 'position' not in result: raise ValueError('Priority change was not confirmed; refresh before retrying')
    elif download_id not in result.get('nzo_ids',[]):
        raise ValueError('Job change was not confirmed; refresh before retrying')
    return result
