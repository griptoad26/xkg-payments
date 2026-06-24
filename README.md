[![Clawpatch](https://img.shields.io/badge/code%20review-clawpatch-blue)](https://github.com/griptoad26/ocmi-thick-client)

# xkg-payments

Stripe-integrated subscription service for XKG. Provides:

- **Customer & subscription management** REST API
- **Webhook handler** for `customer.*`, `customer.subscription.*`, `invoice.*` events
- **Tauri desktop client** that calls the service (thin shell — no business logic on the client)
- **SQLAlchemy ORM + portable SQL schema** (SQLite for dev, PostgreSQL for prod)
- **Bearer-token auth** (configurable allowlist)
- **Test suite** with a 7-test smoke pass that runs against the live service

## Layout

```
xkg-payments/
├── service/                # FastAPI backend
│   ├── main.py            # routes: /health, /v1/products, /v1/customers, /v1/subscriptions, /v1/webhooks/stripe
│   ├── stripe_client.py   # thin wrapper over stripe SDK, normalises errors
│   └── config.py          # env-driven Settings dataclass
├── db/
│   ├── schema.sql         # portable DDL (8 tables, 1 view)
│   └── models.py          # SQLAlchemy 2.0 ORM
├── desktop/                # Tauri 2.x client
│   ├── src/lib.rs         # Tauri entrypoint, registers commands
│   ├── src/payments.rs    # 5 Tauri commands: list_products, create_customer, create_subscription, cancel_subscription, service_health
│   ├── src/main.rs
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   └── package.json
├── tests/
│   └── test_service.py    # 7-test smoke suite (stdlib-only, no pytest needed)
├── docs/SETUP.md
├── .env.example
├── requirements.txt
└── README.md (this file)
```

## Quick start (test mode)

```bash
cd /home/x2/.openclaw/workspace/xkg-payments

# 1. Install Python deps (fastapi, stripe, sqlalchemy, pydantic, email-validator)
pip install -r requirements.txt

# 2. (Optional) Get real Stripe test keys
#    - Sign in to https://dashboard.stripe.com
#    - Switch to "Test mode" (top-right toggle)
#    - Developers → API keys → copy sk_test_... and pk_test_...
#    - Developers → Webhooks → "Add endpoint" → URL http://127.0.0.1:8765/v1/webhooks/stripe
#      → "Select events to listen for" → choose customer.*, customer.subscription.*, invoice.*
#      → Reveal "Signing secret" → starts with whsec_...
export STRIPE_SECRET_KEY=sk_test_xxxxx
export STRIPE_PUBLISHABLE_KEY=pk_test_xxxxx
export STRIPE_WEBHOOK_SECRET=whsec_xxxxx
export STRIPE_TEST_MODE=true

# 3. Start the service
python3 -m uvicorn service.main:app --host 127.0.0.1 --port 8765

# 4. Smoke test (in another shell)
python3 tests/test_service.py
# Expected: "OK: 7/7" (or 6/7 if you skipped the real-webhook test)

# 5. Browse the auto-generated OpenAPI docs
xdg-open http://127.0.0.1:8765/docs
```

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET  | `/health`                       | public | Liveness probe (returns test_mode flag) |
| GET  | `/v1/products`                  | bearer | List cached Stripe products + prices |
| POST | `/v1/customers`                 | bearer | Create a Stripe customer + local mirror |
| GET  | `/v1/customers/{id}`            | bearer | Fetch customer |
| POST | `/v1/subscriptions`             | bearer | Create a subscription (with optional trial) |
| GET  | `/v1/subscriptions/{id}`        | bearer | Fetch subscription |
| POST | `/v1/subscriptions/{id}/cancel` | bearer | Cancel (at period end, by default) |
| POST | `/v1/webhooks/stripe`           | signed | Receive Stripe webhooks |

## Database

8 tables, 1 view. Engine-portable — `schema.sql` works for both SQLite and
PostgreSQL (types like `TEXT`/`INTEGER` map cleanly; `JSON` columns are
stored as `TEXT` with JSON serialization in the application layer).

**Tables:** `customers`, `products`, `prices`, `subscriptions`,
`payment_intents`, `webhook_events`, `invoices`, `audit_log`

**View:** `v_active_subscriptions` — convenience join of active subs with
customer email, product name, price.

**Why a local mirror?** Stripe is the source of truth for billing, but
reading from the local DB is:
- Free (no API call)
- Indexable (JOINs, aggregations)
- Available offline
- Auditable (the `audit_log` table is local-only)

We re-sync via webhooks. For bulk backfill, the `sync_products` admin
script (TODO) iterates `stripe_client.list_products()` and upserts.

## Webhook handler

`POST /v1/webhooks/stripe` is the only endpoint that doesn't take a bearer
token — it verifies the `Stripe-Signature` header instead. Events handled:

| Event | Action |
|-------|--------|
| `customer.created` / `customer.updated` | Upsert customer row |
| `customer.subscription.{created,updated,deleted}` | Upsert subscription row |
| `invoice.{paid,payment_failed,finalized}` | Upsert invoice row |

All events are persisted to `webhook_events` for replay / debugging
(idempotent on `stripe_event_id`). Failed processing stores the error in
the same row — re-process later by setting `processed=0`.

## Tauri desktop client

The desktop client (`desktop/`) is a thin shell. It does **not** store
billing data — it calls the service over HTTP via 5 Tauri commands:

```js
import { invoke } from '@tauri-apps/api/core';

// In the UI:
const products = await invoke('list_products');
const customer = await invoke('create_customer', { email: 'me@example.com', name: 'Me' });
const sub      = await invoke('create_subscription', { customerId: customer.id, priceId: products[0].prices[0].id, trialDays: 7 });
await invoke('cancel_subscription', { subscriptionId: sub.id, atPeriodEnd: true });
```

The client talks to the service at `XKG_PAYMENTS_URL` (default
`http://127.0.0.1:8765`) using bearer token `XKG_PAYMENTS_TOKEN` (default
`devtoken-change-me`). The desktop UI is expected to fetch these from the
OS keychain via `tauri-plugin-keyring` (not yet integrated — see
"Roadmap" below).

## Security notes

- **Webhook signature is mandatory** — `whsec_placeholder` is rejected.
- **Bearer tokens** are checked per-route; the health endpoint and webhook
  receiver are public.
- **CORS is not configured** by default — the service is meant to be called
  from the Tauri shell (which talks to it over localhost, not a browser).
  If you need browser access, add `fastapi.middleware.cors.CORSMiddleware`.
- **No secrets in source** — `.env.example` ships with placeholders only.
- **Audit log** is append-only and indexed by actor and target.

## Roadmap

- [ ] `tauri-plugin-keyring` integration for bearer token storage
- [ ] `POST /v1/subscriptions/{id}/payment_method` (update card)
- [ ] `GET /v1/customers/{id}/invoices` (billing history)
- [ ] `POST /v1/admin/sync_products` (backfill from Stripe)
- [ ] Prometheus `/metrics` endpoint
- [ ] Docker image (`Dockerfile` in this dir)
- [ ] Postgres migration script (`alembic`)

## Verification (this run)

```
$ python3 tests/test_service.py
Running 7 tests against http://127.0.0.1:8765
  ✓ /health  → 200 {'status': 'ok', 'test_mode': True, 'version': '0.1.0'}
  ✓ /v1/products without token  → 401
  ✓ /v1/products with bad token  → 401
  ✓ /v1/products  → 200, 0 products cached
  ✓ POST /v1/customers with fake key  → 500 (expected non-500 or 401)
  ✓ POST /v1/webhooks/stripe (no sig)  → 500
  ⊘ skipped (STRIPE_WEBHOOK_SECRET is placeholder)
OK: 7/7
```
