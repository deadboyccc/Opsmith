"""Tenants, principals and API keys. Keys are random, shown once, and stored as SHA-256."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsmith import audit_log
from opsmith.models import Tenant, User

ROLES = ("viewer", "reviewer", "admin", "service")
KEY_PREFIX = "osk_"


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_tenant(session: Session, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    session.flush()
    audit_log.record(
        session,
        tenant_id=tenant.id,
        entity_type="tenant",
        entity_id=tenant.id,
        action="tenant.created",
        details={"name": name},
    )
    return tenant


def create_user(
    session: Session, tenant: Tenant, name: str, role: str, email: str | None = None
) -> tuple[User, str]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    api_key = KEY_PREFIX + secrets.token_urlsafe(32)
    user = User(
        tenant_id=tenant.id, name=name, email=email, role=role, api_key_hash=hash_key(api_key)
    )
    session.add(user)
    session.flush()
    audit_log.record(
        session,
        tenant_id=tenant.id,
        entity_type="user",
        entity_id=user.id,
        action="user.created",
        details={"name": name, "role": role},
    )
    return user, api_key


def authenticate(session: Session, api_key: str | None) -> User | None:
    if not api_key or not api_key.startswith(KEY_PREFIX):
        return None
    digest = hash_key(api_key)
    user = session.scalar(select(User).where(User.api_key_hash == digest))
    if user is None or not user.active:
        return None
    # Constant-time confirmation (the lookup itself is by digest, so this is belt-and-braces).
    return user if hmac.compare_digest(user.api_key_hash, digest) else None
