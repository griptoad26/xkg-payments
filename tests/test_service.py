"""Smoke tests for xkg-payments service.

Run with:
    python3 -m pytest tests/ -v
or just:
    python3 tests/test_service.py

These exercise the public surface area without needing real Stripe or
x402 keys: the placeholder keys are expected to surface clean error
messages, which is what we're testing.
"""
from __future__ import annotations

import os
import sys
import json

# Ensure service/ and db/ are importable when running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

BASE = os.environ.get("XKG_PAYMENTS_BASE", "http://127.0.0.1:8765")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def test_health() -> None:
    r = requests.get(f"{BASE}/health", timeout=5)
    if r.status_code != 200:
        fail(f"health: HTTP {r.status_code}: {r.text}")
    body = r.json()
    if body.get("status") != "ok":
        fail(f"health: bad body: {body}")
    ok(f"health: 200, test_mode={body.get('test_mode')}")


def test_x402_checkout() -> None:
    r = requests.post(f"{BASE}/v1/checkout", json={
        "method": "x402",
        "amount_cents": 1500,
        "currency": "usd",
        "description": "Test XKG Pro",
        "plan": "pro",
        "customer_email": "test@example.com",
    }, timeout=5)
    if r.status_code != 201:
        fail(f"x402 checkout: HTTP {r.status_code}: {r.text}")
    body = r.json()
    if body.get("method") != "x402":
        fail(f"x402 checkout: method != x402: {body}")
    if body.get("status") != "pending":
        fail(f"x402 checkout: status != pending: {body}")
    pp = body.get("processor_payload") or {}
    if pp.get("x402Version") != 1:
        fail(f"x402 checkout: x402Version != 1: {pp.get('x402Version')}")
    if pp.get("scheme") != "exact":
        fail(f"x402 checkout: scheme != exact: {pp.get('scheme')}")
    accepts = pp.get("accepts") or []
    if not accepts or not accepts[0].get("payTo"):
        fail(f"x402 checkout: no payTo in accepts[0]")
    if not accepts[0].get("maxAmountRequired"):
        fail(f"x402 checkout: no maxAmountRequired")
    ok(f"x402 checkout: id={body['id']}, payment_id={pp['payment_id']}, "
       f"maxAmountRequired={accepts[0]['maxAmountRequired']}")


def test_x402_idempotency_at_ledger() -> None:
    """Two distinct challenges → two ledger entries (correct). Same
    settle call → fails predictably because we can't actually pay."""
    r1 = requests.post(f"{BASE}/v1/checkout", json={
        "method": "x402", "amount_cents": 100, "currency": "usd",
        "description": "idemp test 1", "plan": "test",
    }, timeout=5)
    r2 = requests.post(f"{BASE}/v1/checkout", json={
        "method": "x402", "amount_cents": 100, "currency": "usd",
        "description": "idemp test 2", "plan": "test",
    }, timeout=5)
    if r1.status_code != 201 or r2.status_code != 201:
        fail(f"x402 idempotency checkout: {r1.status_code} / {r2.status_code}")
    b1, b2 = r1.json(), r2.json()
    if b1["id"] == b2["id"]:
        fail(f"x402 idempotency: two challenges returned same id (should be different)")
    if b1["processor_payload"]["payment_id"] == b2["processor_payload"]["payment_id"]:
        fail(f"x402 idempotency: two challenges returned same payment_id (should differ)")
    ok("x402 idempotency: two challenges yield two distinct ledger entries + payment_ids")


def test_x402_settle_bad_tx_hash() -> None:
    """Bogus tx hash → 402 with reason, not 500."""
    # First make a challenge
    r = requests.post(f"{BASE}/v1/checkout", json={
        "method": "x402", "amount_cents": 100, "currency": "usd",
        "description": "settle-bad-tx", "plan": "test",
    }, timeout=5)
    if r.status_code != 201:
        fail(f"x402 settle: setup checkout failed: {r.text}")
    payment_id = r.json()["processor_payload"]["payment_id"]

    r2 = requests.post(f"{BASE}/v1/x402/settle", json={
        "payment_id": payment_id, "tx_hash": "not-a-hash",
    }, timeout=5)
    if r2.status_code != 402:
        fail(f"x402 settle bad tx: expected 402, got {r2.status_code}: {r2.text}")
    ok(f"x402 settle bad tx: HTTP 402 with reason")


def test_x402_settle_unknown_payment() -> None:
    r = requests.post(f"{BASE}/v1/x402/settle", json={
        "payment_id": "x402-fake-XYZ123", "tx_hash": "0x" + "0" * 64,
    }, timeout=5)
    if r.status_code != 404:
        fail(f"x402 settle unknown: expected 404, got {r.status_code}: {r.text}")
    ok("x402 settle unknown payment_id: HTTP 404")


def test_ledger_list_and_get() -> None:
    r = requests.get(f"{BASE}/v1/ledger?limit=5", timeout=5)
    if r.status_code != 200:
        fail(f"ledger list: {r.status_code}: {r.text}")
    body = r.json()
    if body.get("count", 0) < 1:
        fail(f"ledger list: count=0, expected ≥1 from prior tests")
    one = body["data"][0]
    ok(f"ledger list: {body['count']} entries, newest has id={one['id'][:8]}…")

    r2 = requests.get(f"{BASE}/v1/ledger/{one['id']}", timeout=5)
    if r2.status_code != 200:
        fail(f"ledger get: {r2.status_code}: {r2.text}")
    if r2.json()["id"] != one["id"]:
        fail(f"ledger get: id mismatch")
    ok(f"ledger get: round-trip id={one['id'][:8]}…")


def test_ar_open_and_summary() -> None:
    r = requests.get(f"{BASE}/v1/ar/open", timeout=5)
    if r.status_code != 200:
        fail(f"ar open: {r.status_code}: {r.text}")
    body = r.json()
    ok(f"ar open: {body['count']} pending entries (AR worklist)")

    r2 = requests.get(f"{BASE}/v1/ar/summary", timeout=5)
    if r2.status_code != 200:
        fail(f"ar summary: {r2.status_code}: {r2.text}")
    body2 = r2.json()
    by = body2.get("by_status_and_processor") or []
    if not by:
        fail(f"ar summary: empty by_status_and_processor")
    ok(f"ar summary: {len(by)} status/processor combinations, total cents={sum(r['total_cents'] for r in by)}")


def test_stripe_checkout_with_placeholder_key() -> None:
    """Stripe with placeholder keys must surface the auth error as 401/500,
    not 500-with-stacktrace. This validates the error-normalisation path."""
    r = requests.post(f"{BASE}/v1/checkout", json={
        "method": "stripe", "amount_cents": 2500, "currency": "usd",
        "description": "stripe test", "customer_email": "buyer@example.com",
    }, timeout=8)
    if r.status_code not in (401, 500):
        fail(f"stripe checkout: expected 401 or 500 (placeholder key), got {r.status_code}: {r.text}")
    body = r.text
    if "AuthenticationError" not in body and "auth_error" not in body:
        fail(f"stripe checkout: error body doesn't mention auth: {body}")
    ok(f"stripe checkout: HTTP {r.status_code} with auth-error message (placeholder key)")


def test_openapi_lists_all_routes() -> None:
    r = requests.get(f"{BASE}/openapi.json", timeout=5)
    if r.status_code != 200:
        fail(f"openapi: {r.status_code}")
    paths = set(r.json()["paths"].keys())
    expected = {
        "/health", "/v1/checkout", "/v1/x402/settle",
        "/v1/ledger", "/v1/ledger/{entry_id}",
        "/v1/ar/open", "/v1/ar/summary",
        "/v1/customers", "/v1/subscriptions", "/v1/webhooks/stripe",
        "/v1/products",
    }
    missing = expected - paths
    if missing:
        fail(f"openapi missing routes: {missing}")
    ok(f"openapi: all {len(expected)} expected routes present")


def main() -> int:
    tests = [
        test_health,
        test_openapi_lists_all_routes,
        test_x402_checkout,
        test_x402_idempotency_at_ledger,
        test_x402_settle_bad_tx_hash,
        test_x402_settle_unknown_payment,
        test_ledger_list_and_get,
        test_ar_open_and_summary,
        test_stripe_checkout_with_placeholder_key,
    ]
    print(f"Running {len(tests)} smoke tests against {BASE}")
    print("-" * 60)
    for t in tests:
        print(f"\n{t.__name__}:")
        try:
            t()
        except Exception as e:
            fail(f"{t.__name__} raised: {type(e).__name__}: {e}")
    print("\n" + "=" * 60)
    print(f"All {len(tests)} tests passed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
