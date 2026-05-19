from __future__ import annotations

import base64
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import secrets


HASH_PREFIX = 'pbkdf2_sha256'
DEFAULT_ITERATIONS = 260_000


def local_plex_id(username: str) -> str:
    return f"local:{(username or 'admin').strip().lower()}"


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    salt = secrets.token_urlsafe(18)
    digest = pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), iterations)
    encoded = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return f'{HASH_PREFIX}${iterations}${salt}${encoded}'


def verify_password(password: str, password_hash: str) -> bool:
    try:
        prefix, iterations_raw, salt, encoded = (password_hash or '').split('$', 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if prefix != HASH_PREFIX or iterations < 100_000 or not salt or not encoded:
        return False
    digest = pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), iterations)
    candidate = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return compare_digest(candidate, encoded)


def local_auth_configured(values: dict[str, str]) -> bool:
    return bool((values.get('local_auth_username') or '').strip() and (values.get('local_auth_password_hash') or '').strip())
