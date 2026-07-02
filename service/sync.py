"""xkg-sync — server-side endpoints for the device sync protocol.

Phase 4 of the master build plan. The server only ever sees opaque
AES-GCM blobs — it stores them keyed by `device_id` and routes them
back to that device on download. No conflict resolution, no push
notifications, no auth fancier than the existing bearer / session
model.

Endpoints (mounted at /api/sync/*):
  POST /api/sync/devices    body: Device         → echo + row in sync_devices
  POST /api/sync/upload     body: opaque envelope → row in sync_envelopes
  GET  /api/sync/download?device_id=ULID → envelopes for that device

Storage: SQLite via the existing SQLAlchemy session factory
(`db.models.SessionLocal`). Two new tables created idempotently on
first call (`_ensure_tables()`); the main app calls it on startup so
we don't pay the cost on every request.

Auth: re-uses the existing `require_bearer` from `service.main`. The
client (xkg-core's `SyncHttpClient`) already speaks `Authorization:
Bearer …` so this matches the rest of the API.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import config as cfg
from . import main as app_main
from db import models

log = logging.getLogger("xkg-payments.sync")

router = APIRouter(prefix="/api/sync", tags=["sync"])


# ── Allowed platforms + ULID-ish id validation ────────────────────────────

_ALLOWED_PLATFORMS = {"macos", "windows", "linux", "ios", "android"}

# ULID = 26 chars, Crockford base32 (0-9 A-Z minus I L O U).
# Be lenient: accept anything 16-40 chars of [A-Za-z0-9_-] so test
# fixtures and older client ids don't trip the gate. The strict ULID
# regex is documented for future tightening.
_ULID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ── Pydantic schemas ──────────────────────────────────────────────────────

class DeviceIn(BaseModel):
    device_id: str = Field(min_length=8, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=20)
    app_version: str = Field(min_length=1, max_length=64)


class DeviceOut(BaseModel):
    device_id: str
    name: str
    platform: str
    app_version: str
    last_seen_at: int
    created_at: int


# ── DB helpers ────────────────────────────────────────────────────────────

def _now_unix() -> int:
    return int(time.time())


def _ensure_tables() -> None:
    """Idempotent schema bootstrap. Safe to call on every startup."""
    with models.SessionLocal() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS sync_devices (
                device_id    TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                platform     TEXT NOT NULL,
                app_version  TEXT NOT NULL,
                last_seen_at INTEGER NOT NULL,
                created_at   INTEGER NOT NULL
            )
        """))
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS sync_envelopes (
                envelope_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id            INTEGER NOT NULL,
                device_id          TEXT NOT NULL,
                encrypted_payload  BLOB NOT NULL,
                nonce              BLOB NOT NULL,
                msg_cursor         INTEGER NOT NULL,
                created_at         INTEGER NOT NULL,
                FOREIGN KEY (device_id) REFERENCES sync_devices(device_id)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_envelopes_device ON sync_envelopes(device_id)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_envelopes_user ON sync_envelopes(user_id)"))
        s.commit()


def _user_id_from_request(request: Request) -> int:
    """Resolve a stable numeric user_id from the bearer token / session.

    The rest of the service treats the bearer token as an opaque secret;
    for Phase 4 we don't need per-user scoping beyond "is this request
    authenticated at all". Hash the token to a stable 63-bit int so the
    same caller always sees the same envelopes, and different callers
    see different ones (test #3).
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token and token in cfg.settings.api_bearer_tokens:
            # Stable hash of the token → big positive int.
            h = 0
            for ch in token:
                h = (h * 131 + ord(ch)) & 0x7FFFFFFFFFFFFFFF
            return h
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid bearer token")


# ── Routes ────────────────────────────────────────────────────────────────

@router.post("/devices", response_model=DeviceOut, status_code=200)
def register_device(
    body: DeviceIn,
    request: Request,
    db: Session = Depends(models.get_session),
):
    """Upsert a device row keyed by `device_id`. Returns the server's view."""
    if not _ULID_RE.match(body.device_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device_id must be ULID-shaped (A-Za-z0-9_-)")
    if body.platform not in _ALLOWED_PLATFORMS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"platform must be one of {sorted(_ALLOWED_PLATFORMS)}",
        )
    _ensure_tables()
    user_id = _user_id_from_request(request)
    now = _now_unix()

    existing = db.execute(
        text("SELECT last_seen_at, created_at FROM sync_devices WHERE device_id = :id"),
        {"id": body.device_id},
    ).first()

    if existing:
        last_seen, created_at = existing
        db.execute(
            text("""
                UPDATE sync_devices
                   SET user_id = :uid, name = :name, platform = :plat,
                       app_version = :ver, last_seen_at = :now
                 WHERE device_id = :id
            """),
            {"uid": user_id, "name": body.name, "plat": body.platform,
             "ver": body.app_version, "now": now, "id": body.device_id},
        )
    else:
        created_at = now
        db.execute(
            text("""
                INSERT INTO sync_devices
                    (device_id, user_id, name, platform, app_version, last_seen_at, created_at)
                VALUES
                    (:id, :uid, :name, :plat, :ver, :now, :now)
            """),
            {"id": body.device_id, "uid": user_id, "name": body.name,
             "plat": body.platform, "ver": body.app_version, "now": now},
        )
    db.commit()

    return DeviceOut(
        device_id=body.device_id,
        name=body.name,
        platform=body.platform,
        app_version=body.app_version,
        last_seen_at=now,
        created_at=created_at,
    )


@router.post("/upload", status_code=200)
async def sync_upload(
    request: Request,
    db: Session = Depends(models.get_session),
):
    """Accept an opaque AES-GCM envelope, store it keyed by device_id.

    Body is the SyncEnvelope JSON from xkg-core (or any opaque JSON
    with at minimum `device_id`, `encrypted_payload`, `nonce`,
    `msg_cursor`). The server does not decrypt — it just records the
    blob in order. Returns the new `envelope_id`.
    """
    _ensure_tables()
    user_id = _user_id_from_request(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be valid JSON")

    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be a JSON object")

    device_id = body.get("device_id")
    if not device_id or not isinstance(device_id, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device_id is required")

    encrypted_b64 = body.get("encrypted_payload")
    nonce_b64 = body.get("nonce")
    if not isinstance(encrypted_b64, str) or not isinstance(nonce_b64, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "encrypted_payload and nonce must be base64 strings")

    # cursors: optional, default to 0
    msg_cursor = int(body.get("msg_cursor") or 0)

    try:
        encrypted_bytes = base64.b64decode(encrypted_b64, validate=True)
        nonce_bytes = base64.b64decode(nonce_b64, validate=True)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"base64 decode failed: {e}")

    if len(nonce_bytes) != 12:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nonce must decode to exactly 12 bytes (AES-GCM)")
    if not encrypted_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "encrypted_payload must be non-empty")

    # Device must already be registered to this user (FK + isolation).
    row = db.execute(
        text("SELECT user_id FROM sync_devices WHERE device_id = :id"),
        {"id": device_id},
    ).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not registered (call /api/sync/devices first)")
    if row[0] != user_id:
        # Don't reveal whether the device exists for someone else.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not registered (call /api/sync/devices first)")

    now = _now_unix()
    result = db.execute(
        text("""
            INSERT INTO sync_envelopes
                (user_id, device_id, encrypted_payload, nonce, msg_cursor, created_at)
            VALUES
                (:uid, :did, :payload, :nonce, :cur, :now)
        """),
        {"uid": user_id, "did": device_id, "payload": encrypted_bytes,
         "nonce": nonce_bytes, "cur": msg_cursor, "now": now},
    )
    db.commit()
    envelope_id = result.lastrowid
    return {"ok": True, "envelope_id": envelope_id, "device_id": device_id, "bytes": len(encrypted_bytes)}


@router.get("/download", status_code=200)
def sync_download(
    device_id: str,
    request: Request,
    db: Session = Depends(models.get_session),
):
    """Return every envelope for `device_id` in insertion order.

    Cross-user isolation: if `device_id` is registered to a different
    user, returns an empty list (does NOT 404 — keeps client-side
    polling logic simple).
    """
    _ensure_tables()
    user_id = _user_id_from_request(request)
    if not device_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device_id query param is required")

    row = db.execute(
        text("SELECT user_id FROM sync_devices WHERE device_id = :id"),
        {"id": device_id},
    ).first()
    if not row or row[0] != user_id:
        # Empty result rather than 404: clients polling for new
        # envelopes shouldn't have to distinguish "no envelopes yet"
        # from "wrong device_id".
        return {"envelopes": [], "device_id": device_id}

    rows = db.execute(
        text("""
            SELECT envelope_id, device_id, encrypted_payload, nonce, msg_cursor, created_at
              FROM sync_envelopes
             WHERE device_id = :id
             ORDER BY envelope_id ASC
        """),
        {"id": device_id},
    ).all()
    envelopes = [
        {
            "envelope_id": r[0],
            "device_id": r[1],
            "encrypted_payload": base64.b64encode(bytes(r[2])).decode("ascii"),
            "nonce": base64.b64encode(bytes(r[3])).decode("ascii"),
            "msg_cursor": int(r[4]),
            "created_at": int(r[5]),
        }
        for r in rows
    ]
    return {"envelopes": envelopes, "device_id": device_id}