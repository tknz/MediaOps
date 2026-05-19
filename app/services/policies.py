from collections import defaultdict
from sqlalchemy.orm import Session
from ..models import PolicyActionLog, UserBlock, UserPolicy
from .clients import PlexClient


def session_device_identity(session: dict) -> str | None:
    machine_id = str(session.get('machine_identifier') or session.get('machine_id') or '').strip()
    if machine_id:
        return f'machine:{machine_id}'
    parts = [
        session.get('product'),
        session.get('device'),
        session.get('platform'),
        session.get('platform_version'),
        session.get('player'),
    ]
    value = '|'.join(str(part).strip().lower() for part in parts if str(part or '').strip())
    return f'device:{value}' if value else None


def sessions_over_device_limit(items: list[dict], limit: int) -> list[dict]:
    allowed = set()
    denied = set()
    targets = []
    for session in items:
        identity = session_device_identity(session)
        if not identity:
            continue
        if identity in allowed:
            continue
        if identity in denied:
            continue
        if len(allowed) < limit:
            allowed.add(identity)
            continue
        denied.add(identity)
        targets.extend(i for i in items if session_device_identity(i) == identity)
    return targets


async def enforce_policies(db: Session, client: PlexClient, sessions: list[dict]):
    policies = {p.username.lower(): p for p in db.query(UserPolicy).all()}
    blocks = {}
    for b in db.query(UserBlock).filter(UserBlock.active == True).all():
        blocks.setdefault(b.username.lower(), []).append(b)
    by_user = defaultdict(list)
    for session in sessions:
        by_user[(session.get('user') or '').lower()].append(session)

    for uname, items in by_user.items():
        policy = policies.get(uname)
        reason = None
        targets = []
        user_blocks = blocks.get(uname, [])
        for block in user_blocks:
            if block.block_type == 'ip':
                matches = [i for i in items if block.value in {i.get('player_address'), i.get('remote_public_address')}]
                if matches:
                    reason = block.message or 'The IP address you are connecting from is banned. Please contact your server administrator.'
                    targets = matches
                    break
            if block.block_type == 'device':
                # Only block on Plex machineIdentifier: it is the stable client/device key.
                matches = [i for i in items if block.value and block.value == i.get('machine_identifier')]
                if matches:
                    reason = block.message or 'The device you are connecting from is banned. Please contact your server administrator.'
                    targets = matches
                    break
        if reason:
            pass
        elif policy and policy.blocked:
            reason = policy.block_message or 'Your streaming access is currently banned. Please contact your server administrator.'
            targets = items
        elif policy and policy.max_concurrent_streams and len(items) > policy.max_concurrent_streams:
            reason = f'Your account is already using the maximum allowed number of streams ({policy.max_concurrent_streams}). Please stop another stream or contact your server administrator.'
            targets = items[policy.max_concurrent_streams:]
        elif policy and policy.max_concurrent_devices:
            targets = sessions_over_device_limit(items, policy.max_concurrent_devices)
            if targets:
                reason = f'Your account is already using the maximum allowed number of devices ({policy.max_concurrent_devices}). Please stop another stream or contact your server administrator.'
        elif policy and policy.max_public_ips:
            public_ips = {s.get('remote_public_address') for s in items if s.get('remote_public_address')}
            if len(public_ips) > policy.max_public_ips:
                reason = f'Your account is streaming from too many different public IP addresses at the same time (limit: {policy.max_public_ips}). Please stop another stream or contact your server administrator.'
                targets = items
        if reason:
            for session in targets:
                sid = session.get('session_id')
                if sid:
                    await client.terminate_session(sid, reason)
                    db.add(PolicyActionLog(username=session.get('user') or uname, session_id=sid, action='terminate', reason=reason))
    db.commit()
