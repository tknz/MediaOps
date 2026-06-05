from __future__ import annotations

from datetime import datetime
import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import IntegrationToken


DEFAULT_HOMEASSISTANT_SCOPES = 'ha.read integrations.read ha.write ha.admin'


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return f"mo_{secrets.token_urlsafe(36)}"


def issue_integration_token(db: Session, name: str, scopes: str, created_by: str | None = None) -> tuple[IntegrationToken, str]:
    token = generate_token()
    row = IntegrationToken(
        name=(name or 'Home Assistant').strip() or 'Home Assistant',
        token_hash=token_hash(token),
        scopes=(scopes or DEFAULT_HOMEASSISTANT_SCOPES).strip() or DEFAULT_HOMEASSISTANT_SCOPES,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, token


def active_integration_tokens(db: Session) -> list[IntegrationToken]:
    return db.scalars(select(IntegrationToken).where(IntegrationToken.revoked_at.is_(None)).order_by(IntegrationToken.created_at.desc())).all()


def revoke_integration_token(db: Session, token_id: int) -> bool:
    row = db.get(IntegrationToken, token_id)
    if not row or row.revoked_at:
        return False
    row.revoked_at = datetime.utcnow()
    db.commit()
    return True


def authenticate_integration_token(db: Session, token: str) -> IntegrationToken | None:
    digest = token_hash(token)
    row = db.scalar(select(IntegrationToken).where(IntegrationToken.token_hash == digest, IntegrationToken.revoked_at.is_(None)))
    if row:
        row.last_used_at = datetime.utcnow()
        db.commit()
    return row
