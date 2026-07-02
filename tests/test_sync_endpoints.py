"""Tests for xkg-sync endpoints (Phase 4).

Run with:
    cd /home/x2/.openclaw/workspace/xkg-payments
    python3 -m pytest tests/test_sync_endpoints.py -v

Or as a standalone script:
    python3 tests/test_sync_endpoints.py

These run in-process against a FastAPI TestClient so they don't need
the uvicorn server to be up. Each test uses a fresh temporary SQLite
file to avoid cross-test contamination and to keep them self-contained
(no global state in the sync module).

What we cover (the 3 tests the brief asked for):
  1. test_register_device           POST /api/sync/devices → 200, row in sync_devices
  2. test_upload_then_download      register → upload → download → envelopes in order
  3. test_cross_user_isolation      device A's envelopes not visible to user B

Plus a few defensive extras that caught real bugs during development:
  4. test_upload_rejects_bad_base64 (400 on garbage)
  5. test_upload_requires_registered_device (404 on unregistered device_id)
  6. test_download_unknown_device_returns_empty (not 404)
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile

# Ensure service/ and db/ are importable when running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


# ── Per-test isolated FastAPI app ─────────────────────────────────────────
# FastAPI's TestClient wires up the whole app, which means init_db()
# + auth tables + sync tables all run on import. To keep tests hermetic
# we point each test at a fresh temp SQLite file via DATABASE_URL.

def _make_app_and_client():
    from fastapi.testclient import TestClient

    fd, db_path = tempfile.mkstemp(prefix="xkg-sync-test-", suffix=".db")
    os.close(fd)
    # Remove the empty file so SQLite creates a fresh one when the
    # engine opens it.
    os.unlink(db_path)

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    # Make sure settings pick this up at import time.
    os.environ["API_BEARER_TOKENS"] = "test-tok-alice,test-tok-bob"
    # FastAPI settings is cached at import; nuke it.
    import importlib
    from service import config as cfg_mod
    importlib.reload(cfg_mod)
    from service import main as main_mod
    importlib.reload(main_mod)
    from db import models
    importlib.reload(models)
    # Re-wire checkout / auth / sync against the reloaded modules so
    # they pick up the new DATABASE_URL.
    from service import checkout, auth as auth_mod, sync as sync_mod
    importlib.reload(checkout)
    importlib.reload(auth_mod)
    importlib.reload(sync_mod)
    # Re-include routers (main_mod already imported them once; do it
    # again to pick up the reloaded router objects).
    main_mod.app.include_router(checkout.router)
    main_mod.app.include_router(auth_mod.router)
    main_mod.app.include_router(sync_mod.router)
    main_mod.models.init_db()
    auth_mod._ensure_tables()
    sync_mod._ensure_tables()

    client = TestClient(main_mod.app)
    return client, db_path, main_mod, sync_mod, models


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ────────────────────────────────────────────────────────────────

def test_register_device() -> None:
    print("test_register_device:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        device_id = "01HABCDEFGHJKMNPQRSTVWXYZ"
        r = client.post(
            "/api/sync/devices",
            json={
                "device_id": device_id,
                "name": "x2-mbp",
                "platform": "macos",
                "app_version": "xkg-desktop/0.2.0",
            },
            headers=_hdr("test-tok-alice"),
        )
        if r.status_code != 200:
            fail(f"register_device: HTTP {r.status_code}: {r.text}")
        body = r.json()
        if body.get("device_id") != device_id:
            fail(f"register_device: device_id mismatch: {body}")
        for f in ("name", "platform", "app_version", "last_seen_at", "created_at"):
            if f not in body:
                fail(f"register_device: missing field {f!r} in response: {body}")
        if body["platform"] != "macos":
            fail(f"register_device: platform not echoed correctly: {body}")

        # Verify row in sync_devices.
        with models.SessionLocal() as s:
            row = s.execute(
                __import__("sqlalchemy").text(
                    "SELECT device_id, name, platform, app_version FROM sync_devices WHERE device_id = :id"
                ),
                {"id": device_id},
            ).first()
        if not row:
            fail(f"register_device: no row in sync_devices for {device_id!r}")
        if row[0] != device_id or row[1] != "x2-mbp" or row[2] != "macos":
            fail(f"register_device: row contents wrong: {row}")
        ok(f"register_device: 200, row persisted (platform={row[2]}, version={row[3]})")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def test_upload_then_download() -> None:
    print("test_upload_then_download:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        device_id = "01HDEVICE001ABCDEFGHJKMNP"
        # Register
        rr = client.post(
            "/api/sync/devices",
            json={"device_id": device_id, "name": "dev-1", "platform": "linux",
                  "app_version": "xkg-desktop/0.2.0"},
            headers=_hdr("test-tok-alice"),
        )
        if rr.status_code != 200:
            fail(f"register: {rr.status_code}: {rr.text}")

        # Upload 3 envelopes in sequence; cursor strictly increasing.
        envelope_payloads = []
        for i, (payload, nonce, cursor) in enumerate([
            (b"\xaa" * 32, b"\x00" * 12, 100),
            (b"\xbb" * 32, b"\x11" * 12, 200),
            (b"\xcc" * 32, b"\x22" * 12, 300),
        ]):
            r = client.post(
                "/api/sync/upload",
                json={
                    "device_id": device_id,
                    "encrypted_payload": base64.b64encode(payload).decode("ascii"),
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "msg_cursor": cursor,
                },
                headers=_hdr("test-tok-alice"),
            )
            if r.status_code != 200:
                fail(f"upload #{i}: HTTP {r.status_code}: {r.text}")
            body = r.json()
            if not body.get("ok"):
                fail(f"upload #{i}: ok != True: {body}")
            if "envelope_id" not in body:
                fail(f"upload #{i}: missing envelope_id: {body}")
            envelope_payloads.append((payload, nonce, cursor, body["envelope_id"]))

        # Download
        rd = client.get(
            f"/api/sync/download?device_id={device_id}",
            headers=_hdr("test-tok-alice"),
        )
        if rd.status_code != 200:
            fail(f"download: HTTP {rd.status_code}: {rd.text}")
        body = rd.json()
        envs = body.get("envelopes")
        if not isinstance(envs, list):
            fail(f"download: envelopes not a list: {body}")
        if len(envs) != 3:
            fail(f"download: expected 3 envelopes, got {len(envs)}")
        # Verify order is by envelope_id ASC and the payloads round-trip.
        prev_id = 0
        for i, (orig_payload, orig_nonce, orig_cursor, orig_id) in enumerate(envelope_payloads):
            env = envs[i]
            if env["envelope_id"] != orig_id:
                fail(f"envelope #{i}: id mismatch, expected {orig_id} got {env['envelope_id']}")
            if env["envelope_id"] <= prev_id:
                fail(f"envelope #{i}: not in ascending order: {prev_id} → {env['envelope_id']}")
            prev_id = env["envelope_id"]
            if base64.b64decode(env["encrypted_payload"]) != orig_payload:
                fail(f"envelope #{i}: encrypted_payload round-trip mismatch")
            if base64.b64decode(env["nonce"]) != orig_nonce:
                fail(f"envelope #{i}: nonce round-trip mismatch")
            if env["msg_cursor"] != orig_cursor:
                fail(f"envelope #{i}: msg_cursor mismatch, expected {orig_cursor} got {env['msg_cursor']}")
        ok(f"upload_then_download: 3 envelopes uploaded, downloaded in insertion order, "
           f"payloads + nonces + cursors round-trip exactly")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def test_cross_user_isolation() -> None:
    print("test_cross_user_isolation:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        device_id = "01HDEVICE002ABCDEFGHJKMNP"
        # Alice registers + uploads 2 envelopes
        rr = client.post(
            "/api/sync/devices",
            json={"device_id": device_id, "name": "alice-laptop", "platform": "windows",
                  "app_version": "xkg-desktop/0.2.0"},
            headers=_hdr("test-tok-alice"),
        )
        if rr.status_code != 200:
            fail(f"alice register: {rr.status_code}: {rr.text}")
        for i, cursor in enumerate([10, 20]):
            r = client.post(
                "/api/sync/upload",
                json={
                    "device_id": device_id,
                    "encrypted_payload": base64.b64encode(b"\xee" * 16).decode("ascii"),
                    "nonce": base64.b64encode(b"\x33" * 12).decode("ascii"),
                    "msg_cursor": cursor,
                },
                headers=_hdr("test-tok-alice"),
            )
            if r.status_code != 200:
                fail(f"alice upload #{i}: {r.status_code}: {r.text}")

        # Bob downloads Alice's device — should see zero envelopes,
        # not the 2 she just uploaded.
        rd = client.get(
            f"/api/sync/download?device_id={device_id}",
            headers=_hdr("test-tok-bob"),
        )
        if rd.status_code != 200:
            fail(f"bob download: HTTP {rd.status_code}: {rd.text}")
        body = rd.json()
        if body.get("envelopes") != []:
            fail(f"bob should see 0 envelopes, got {len(body.get('envelopes', []))}: {body}")

        # Bob also can't upload to Alice's device (404, not 200)
        ru = client.post(
            "/api/sync/upload",
            json={
                "device_id": device_id,
                "encrypted_payload": base64.b64encode(b"\xff" * 16).decode("ascii"),
                "nonce": base64.b64encode(b"\x44" * 12).decode("ascii"),
                "msg_cursor": 99,
            },
            headers=_hdr("test-tok-bob"),
        )
        if ru.status_code != 404:
            fail(f"bob upload: expected 404, got {ru.status_code}: {ru.text}")

        # Alice can still see her own envelopes
        rd2 = client.get(
            f"/api/sync/download?device_id={device_id}",
            headers=_hdr("test-tok-alice"),
        )
        if rd2.status_code != 200:
            fail(f"alice re-download: {rd2.status_code}: {rd2.text}")
        if len(rd2.json().get("envelopes", [])) != 2:
            fail(f"alice should still see her 2 envelopes, got: {rd2.json()}")

        ok(f"cross_user_isolation: bob sees 0 of alice's 2 envelopes, "
           f"bob can't upload to alice's device, alice still sees all 2")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def test_upload_rejects_bad_base64() -> None:
    print("test_upload_rejects_bad_base64:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        device_id = "01HDEVICE003ABCDEFGHJKMNP"
        client.post(
            "/api/sync/devices",
            json={"device_id": device_id, "name": "dev", "platform": "linux",
                  "app_version": "xkg-desktop/0.2.0"},
            headers=_hdr("test-tok-alice"),
        )

        # Bad base64
        r = client.post(
            "/api/sync/upload",
            json={
                "device_id": device_id,
                "encrypted_payload": "!!!not-base64!!!",
                "nonce": base64.b64encode(b"\x00" * 12).decode("ascii"),
                "msg_cursor": 1,
            },
            headers=_hdr("test-tok-alice"),
        )
        if r.status_code != 400:
            fail(f"bad base64: expected 400, got {r.status_code}: {r.text}")

        # nonce not 12 bytes
        r2 = client.post(
            "/api/sync/upload",
            json={
                "device_id": device_id,
                "encrypted_payload": base64.b64encode(b"\xaa" * 16).decode("ascii"),
                "nonce": base64.b64encode(b"\x00" * 5).decode("ascii"),
                "msg_cursor": 1,
            },
            headers=_hdr("test-tok-alice"),
        )
        if r2.status_code != 400:
            fail(f"bad nonce length: expected 400, got {r2.status_code}: {r2.text}")

        ok(f"upload_rejects_bad_base64: bad payload → 400, bad nonce length → 400")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def test_upload_requires_registered_device() -> None:
    print("test_upload_requires_registered_device:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        # No /api/sync/devices call first.
        r = client.post(
            "/api/sync/upload",
            json={
                "device_id": "01HNOTREGISTEREDABCDEFGHJ",
                "encrypted_payload": base64.b64encode(b"\xaa" * 16).decode("ascii"),
                "nonce": base64.b64encode(b"\x00" * 12).decode("ascii"),
                "msg_cursor": 1,
            },
            headers=_hdr("test-tok-alice"),
        )
        if r.status_code != 404:
            fail(f"unregistered device: expected 404, got {r.status_code}: {r.text}")
        ok("upload_requires_registered_device: 404 when device not in sync_devices")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def test_download_unknown_device_returns_empty() -> None:
    print("test_download_unknown_device_returns_empty:")
    client, db_path, main_mod, sync_mod, models = _make_app_and_client()
    try:
        r = client.get(
            "/api/sync/download?device_id=01HNEVERSEENDEVICEABCDEF",
            headers=_hdr("test-tok-alice"),
        )
        if r.status_code != 200:
            fail(f"unknown device: expected 200, got {r.status_code}: {r.text}")
        body = r.json()
        if body.get("envelopes") != []:
            fail(f"unknown device: expected empty list, got: {body}")
        ok("download_unknown_device_returns_empty: 200 with envelopes=[] (not 404)")
    finally:
        try:
            os.unlink(db_path)
        except FileNotFoundError:
            pass


def main() -> int:
    tests = [
        test_register_device,
        test_upload_then_download,
        test_cross_user_isolation,
        test_upload_rejects_bad_base64,
        test_upload_requires_registered_device,
        test_download_unknown_device_returns_empty,
    ]
    print(f"Running {len(tests)} sync-endpoint tests (in-process FastAPI TestClient)")
    print("-" * 60)
    failures = 0
    for t in tests:
        try:
            t()
        except SystemExit as e:
            failures += 1
        except Exception as e:
            print(f"  [FAIL] {t.__name__} raised: {type(e).__name__}: {e}")
            failures += 1
    print("\n" + "=" * 60)
    if failures:
        print(f"{failures}/{len(tests)} tests FAILED")
        return 1
    print(f"All {len(tests)} tests passed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())