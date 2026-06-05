from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User
from .services.integration_tokens import authenticate_integration_token

ADMIN_SCOPE = '*'


@dataclass(frozen=True)
class AuthContext:
    username: str
    is_admin: bool = False
    scopes: frozenset[str] = frozenset()
    user: User | None = None
    method: str = 'session'

    def has_scope(self, scope: str) -> bool:
        return self.is_admin or ADMIN_SCOPE in self.scopes or scope in self.scopes


@dataclass(frozen=True)
class TokenAuth:
    token: str
    username: str
    is_admin: bool
    scopes: frozenset[str]


def _split_entries(value: str) -> list[str]:
    return [entry.strip() for entry in value.replace('\n', ';').split(';') if entry.strip()]


def _split_scopes(value: str) -> frozenset[str]:
    scopes = set()
    for chunk in value.replace(',', ' ').split():
        scope = chunk.strip()
        if scope:
            scopes.add(scope)
    return frozenset(scopes)


def configured_api_tokens() -> list[TokenAuth]:
    tokens: list[TokenAuth] = []
    admin_token = settings.api_admin_token.strip()
    if admin_token and _valid_token(admin_token):
        tokens.append(TokenAuth(admin_token, 'api-admin', True, frozenset({ADMIN_SCOPE})))

    for entry in _split_entries(settings.api_tokens):
        token, sep, metadata = entry.partition('=')
        token = token.strip()
        if not sep or not token or not _valid_token(token):
            continue

        parts = [part.strip() for part in metadata.split(':') if part.strip()]
        username = parts[0] if parts else 'api-token'
        flags = {part.lower() for part in parts[1:]}
        is_admin = 'admin' in flags
        scope_text = ' '.join(part for part in parts[1:] if part.lower() != 'admin')
        scopes = _split_scopes(scope_text)
        if is_admin:
            scopes = frozenset({*scopes, ADMIN_SCOPE})
        tokens.append(TokenAuth(token, username, is_admin, scopes))
    return tokens


def _valid_token(token: str) -> bool:
    return token.startswith('mo_') and len(token) >= 46


def bearer_token(request: Request) -> str | None:
    authorization = request.headers.get('authorization', '')
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        return None
    return token.strip()


def token_auth_context(request: Request, db: Session | None = None) -> AuthContext | None:
    token = bearer_token(request)
    if not token:
        return None
    if db is not None:
        integration = authenticate_integration_token(db, token)
        if integration:
            return AuthContext(
                username=integration.name,
                is_admin=False,
                scopes=_split_scopes(integration.scopes),
                method='integration-token',
            )
    for configured in configured_api_tokens():
        if compare_digest(token, configured.token):
            return AuthContext(
                username=configured.username,
                is_admin=configured.is_admin,
                scopes=configured.scopes,
                method='bearer',
            )
    return None


def session_auth_context(request: Request, db: Session) -> AuthContext | None:
    plex_id = request.session.get('plex_id')
    if not plex_id:
        return None
    user = db.scalar(select(User).where(User.plex_id == str(plex_id)))
    if not user:
        return None
    scopes = frozenset({ADMIN_SCOPE}) if user.is_admin else frozenset()
    return AuthContext(user.username, bool(user.is_admin), scopes, user, 'session')


def auth_context(request: Request, db: Session = Depends(get_db)) -> AuthContext | None:
    return token_auth_context(request, db) or session_auth_context(request, db)


def require_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    context = auth_context(request, db)
    if not context:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Bearer'},
        )
    return context


def require_admin(context: AuthContext = Depends(require_auth)) -> AuthContext:
    if not context.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin access required')
    return context


def require_scope(scope: str) -> Callable[[AuthContext], AuthContext]:
    def dependency(context: AuthContext = Depends(require_auth)) -> AuthContext:
        if not context.has_scope(scope):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f'Scope required: {scope}')
        return context

    return dependency
