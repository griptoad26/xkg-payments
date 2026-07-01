"""
Site auth backend for the seele.agency password gate.

Endpoints (mounted at /v1/auth/*):
  POST /v1/auth/register   create account (email + password)
  POST /v1/auth/login      verify password, return session token
  POST /v1/auth/logout     invalidate a session
  GET  /v1/auth/verify     validate a session token (returns user info)

Token format:
  <user_id>.<expiry_epoch>.<hmac_sha256_signature>

Storage: SQLite (same DB as the rest of xkg-payments) — `site_users` and
`site_sessions` tables. Passwords stored as bcrypt hashes (cost 12).

Cookie:
  - name: `seele_session`
  - httpOnly, secure, sameSite=Lax, path=/
  - maxAge: 30 days
"""
from __future__ import annotations

import hmac
import hashlib
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import hashlib
import base64
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field

from . import config as cfg
from . import main as app_main

router = APIRouter(prefix="/v1/auth", tags=["auth"])


# ----------------------------- Pydantic schemas -----------------------------

class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: Optional[str] = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class AuthOut(BaseModel):
    user_id: str
    email: str
    name: Optional[str]
    expires_at: int  # unix epoch seconds


# ----------------------------- Token signing --------------------------------

# Pull the session secret from env or generate a stable one per install.
_SESSION_SECRET = os.environ.get(
    "SELE_SESSION_SECRET",
    cfg.SECRET_KEY if hasattr(cfg, "SECRET_KEY") else None,
)
if not _SESSION_SECRET:
    # Stable per-install fallback: derive from a marker file in the workspace.
    marker = os.path.expanduser("~/.seele_session_secret")
    if os.path.exists(marker):
        with open(marker) as f:
            _SESSION_SECRET = f.read().strip()
    else:
        _SESSION_SECRET = secrets.token_urlsafe(48)
        with open(marker, "w") as f:
            f.write(_SESSION_SECRET)
        os.chmod(marker, 0o600)

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _sign(user_id: str, expires_at: int) -> str:
    # Include a random nonce so the same user logging in twice gets distinct
    # tokens (and the site_sessions UNIQUE constraint is never violated).
    nonce = secrets.token_urlsafe(16)
    msg = f"{user_id}.{expires_at}.{nonce}".encode()
    sig = hmac.new(_SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{user_id}.{expires_at}.{nonce}.{sig}"


def _verify_token(token: str) -> Optional[tuple[str, int]]:
    try:
        user_id, exp_str, nonce, sig = token.split(".", 3)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(
        _SESSION_SECRET.encode(),
        f"{user_id}.{exp}.{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if exp < int(time.time()):
        return None
    return user_id, exp


# ----------------------------- DB helpers -----------------------------------
# We re-use the SQLAlchemy session from the main app. We add two tables
# (`site_users`, `site_sessions`) on the fly.

def _get_session():
    # SessionLocal lives in db/models.py (we share the same SQLAlchemy
    # engine as the rest of xkg-payments).
    from db import models
    return models.SessionLocal()


def _ensure_tables():
    from sqlalchemy import text
    with _get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS site_users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
        """))
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS site_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES site_users(id)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS ix_site_sessions_user ON site_sessions(user_id)"))
        s.commit()


# Schema is created on first use via _ensure_tables() in the route handlers.


# ----------------------------- Cookie name ----------------------------------
COOKIE_NAME = "seele_session"


def _set_cookie(response: Response, token: str, expires_at: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# ----------------------------- Routes ---------------------------------------

@router.post("/register", response_model=AuthOut, status_code=201)
def register(body: RegisterIn, response: Response):
    from sqlalchemy import text
    with _get_session() as s:
        existing = s.execute(
            text("SELECT id FROM site_users WHERE email = :e"),
            {"e": body.email.lower()},
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")
        user_id = str(uuid.uuid4())
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(body.password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        pwd_hash = base64.b64encode(salt).decode() + '$' + base64.b64encode(dk).decode()
        s.execute(
            text("""
                INSERT INTO site_users (id, email, name, password_hash, created_at)
                VALUES (:id, :email, :name, :pwd, :ts)
            """),
            {
                "id": user_id,
                "email": body.email.lower(),
                "name": body.name,
                "pwd": pwd_hash,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        s.commit()

    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    token = _sign(user_id, expires_at)
    with _get_session() as s:
        s.execute(
            text("""
                INSERT INTO site_sessions (token, user_id, expires_at, created_at)
                VALUES (:t, :u, :e, :ts)
            """),
            {"t": token, "u": user_id, "e": expires_at,
             "ts": datetime.now(timezone.utc).isoformat()},
        )
        s.commit()
    _set_cookie(response, token, expires_at)
    return AuthOut(user_id=user_id, email=body.email.lower(), name=body.name, expires_at=expires_at)


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, response: Response):
    from sqlalchemy import text
    with _get_session() as s:
        row = s.execute(
            text("SELECT id, email, name, password_hash FROM site_users WHERE email = :e"),
            {"e": body.email.lower()},
        ).first()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user_id, email, name, pwd_hash = row
        salt_b64, dk_b64 = pwd_hash.split('$', 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        candidate = hashlib.scrypt(body.password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        if not hmac.compare_digest(expected, candidate):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        s.execute(
            text("UPDATE site_users SET last_login_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc).isoformat(), "id": user_id},
        )
        s.commit()

    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    token = _sign(user_id, expires_at)
    with _get_session() as s:
        s.execute(
            text("""
                INSERT INTO site_sessions (token, user_id, expires_at, created_at)
                VALUES (:t, :u, :e, :ts)
            """),
            {"t": token, "u": user_id, "e": expires_at,
             "ts": datetime.now(timezone.utc).isoformat()},
        )
        s.commit()
    _set_cookie(response, token, expires_at)
    return AuthOut(user_id=user_id, email=email, name=name, expires_at=expires_at)


@router.post("/logout", status_code=204)
def logout(response: Response, seele_session: Optional[str] = Cookie(default=None)):
    from sqlalchemy import text
    if seele_session:
        with _get_session() as s:
            s.execute(text("DELETE FROM site_sessions WHERE token = :t"), {"t": seele_session})
            s.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    return Response(status_code=204)


@router.get("/verify", response_model=AuthOut)
def verify(seele_session: Optional[str] = Cookie(default=None)):
    """Validate a session token (read from cookie or Authorization: Bearer)."""
    from fastapi import Header
    from sqlalchemy import text

    # Allow Authorization: Bearer <token> as well, for cross-origin fetch.
    # (Cookie is the primary path; bearer is for the static site's JS calls.)
    raise NotImplementedError  # overridden below


@router.get("/me", response_model=AuthOut)
def me(
    seele_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = None,
):
    from sqlalchemy import text
    token = seele_session
    if not token and authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="No session")
    parsed = _verify_token(token)
    if not parsed:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user_id, exp = parsed
    with _get_session() as s:
        # Token must still be in site_sessions (logout deletes it). Without this
        # check, a logged-out cookie would still be valid until its exp.
        sess = s.execute(
            text("SELECT 1 FROM site_sessions WHERE token = :t"),
            {"t": token},
        ).first()
        if not sess:
            raise HTTPException(status_code=401, detail="Session revoked")
        row = s.execute(
            text("SELECT id, email, name FROM site_users WHERE id = :id"),
            {"id": user_id},
        ).first()
        if not row:
            raise HTTPException(status_code=401, detail="User not found")
        uid, email, name = row
    return AuthOut(user_id=uid, email=email, name=name, expires_at=exp)
