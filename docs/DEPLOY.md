# xkg-payments — Deployment

## What this is

A public payment processor designed for future customer use (NOT exposed
via Tailscale). Single binary, single port. Handles:

- **Card** via Stripe (test or live keys)
- **Crypto** via x402 (USDC on Base, default testnet = `base-sepolia`)
- **LemonSqueezy** (P3, stub returns 501)
- **PayPal** (P4, stub returns 501)
- **Amazon Pay** (P5, stub returns 501)

All payments land in a single **AR-ready ledger** table (`ledger_entries`),
regardless of processor. AR queries (`/v1/ar/open`, `/v1/ar/summary`)
work across all processors from day one.

## Layout

```
service/main.py            # FastAPI entrypoint
service/checkout.py        # /v1/checkout dispatcher + ledger writer
service/stripe_client.py   # Stripe SDK wrapper (PaymentIntent + customers)
service/x402.py            # x402 challenge + on-chain verify
db/models.py               # SQLAlchemy 2.0 ORM (LedgerEntry is the AR table)
db/schema.sql              # canonical schema (mirrors models.py)
pay.html                   # Public checkout page (5 buttons)
tests/test_service.py      # 9 smoke tests, run against live service
```

## Run locally

```bash
cd /home/x2/.openclaw/workspace/xkg-payments
python3 -m uvicorn service.main:app --host 0.0.0.0 --port 8765
```

In another terminal:
```bash
curl http://127.0.0.1:8765/health
# {"status":"ok","test_mode":true,"version":"0.2.0"}

python3 tests/test_service.py
# 9 tests pass
```

## Expose publicly (NOT via Tailscale)

Two options:

### Option A: Cloudflare Worker in front of xkg-payments (recommended)

Cloudflare Worker at `seele.agency` proxies `/api/pay/*` → the service on
the host. This keeps the service behind the firewall while exposing only
the API surface.

The existing `xkg-stripe/reverse_proxy.py` shows the pattern for
on-host routing. For Cloudflare:

1. Worker at `seele.agency/api/pay/*`:
   ```js
   export default {
     async fetch(request) {
       const url = new URL(request.url);
       url.hostname = '100.112.11.35';  // x2-nuc LAN IP, NOT tailnet
       url.port = '8765';
       return fetch(url, { method: request.method, headers: request.headers, body: request.body });
     }
   }
   ```
2. DNS: `seele.agency` → Cloudflare (already configured).
3. The static `pay.html` lives at `seele.agency/pay.html` and calls
   `window.location.origin + '/api/pay'`.

### Option B: Direct public port (simpler, less safe)

Open port 8765 on the firewall and DNS-point a subdomain to the host's
public IP. Not recommended — Cloudflare Worker is the right layer.

## Environment variables

Copy `.env.example` to `.env` and set real values:

| Var | Default | Required for prod? |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_placeholder` | YES (real `sk_live_*`) |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_placeholder` | YES (real `pk_live_*`) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_placeholder` | YES (from Stripe dashboard) |
| `STRIPE_TEST_MODE` | `true` | `false` for prod |
| `HOST` | `127.0.0.1` | `0.0.0.0` if behind a worker |
| `PORT` | `8765` | whatever the proxy expects |
| `DATABASE_URL` | `sqlite:///./xkg-payments.db` | `postgresql://…` for prod |
| `API_BEARER_TOKENS` | `devtoken-change-me` | comma-sep list, rotate |
| `X402_WALLET` | testnet treasury | real wallet address |
| `X402_NETWORK` | `base-sepolia` | `base` for mainnet |

## Status of each processor

| Processor | Status | Notes |
|---|---|---|
| Stripe | ✅ Working | Test or live keys. PaymentIntent + customer + subscription. |
| x402 | ✅ Working | Base Sepolia testnet. Real on-chain verification. |
| LemonSqueezy | 🟡 P3 stub | Returns 501; spec'd in `service/checkout.py` dispatcher. |
| PayPal | 🟡 P4 stub | Returns 501; spec'd in dispatcher. |
| Amazon Pay | 🟡 P5 stub | Returns 501; spec'd in dispatcher. |

## AR-ready ledger

The `ledger_entries` table is the source of truth for every dollar in
or out. Designed so an AR system can plug in as a query layer, not a
migration. Fields:

```
id                local UUID
processor         stripe | x402 | lemonsqueezy | paypal | amazonpay
external_id       processor's reference (idempotent key)
status            pending | succeeded | refunded | void | failed
amount_cents      gross in smallest currency unit
currency          ISO 4217
customer_id       FK → customers (NULL for guest)
customer_email    denormalized for AR reports
invoice_number    INV-2026-0001 style, allocated on success
description       what was purchased
metadata          JSON blob for processor-specific fields
due_at            when payment is expected (for AR aging)
paid_at           when payment succeeded
refunded_at       when refund was issued
refund_reason
created_at, updated_at
```

AR endpoints:
- `GET /v1/ar/open` — pending + due (the worklist)
- `GET /v1/ar/summary` — total receivable by status × processor
- `GET /v1/ledger` — full ledger with filters (`?status=`, `?processor=`, `?customer_email=`)
- `GET /v1/ledger/{id}` — single entry

## Going from test → production

1. Set `STRIPE_SECRET_KEY=sk_live_…`, `STRIPE_PUBLISHABLE_KEY=pk_live_…`
2. Set `STRIPE_TEST_MODE=false`
3. Set `STRIPE_WEBHOOK_SECRET=whsec_…` from Stripe dashboard
4. Set `DATABASE_URL=postgresql://…` (SQLite is dev-only)
5. Set real `X402_WALLET` (real ETH address on Base mainnet)
6. Set `X402_NETWORK=base` for mainnet
7. Rotate `API_BEARER_TOKENS` — the default `devtoken-change-me` MUST change
8. Restart the service
9. Hit `/health` — `test_mode` should now be `false`
10. Smoke-test the live site end-to-end

## What's still TODO (next session)

- P3: LemonSqueezy client wrapper (`service/lemonsqueezy.py`)
- P4: PayPal Orders v2 client (`service/paypal.py`)
- P5: Amazon Pay v2 client (`service/amazonpay.py`)
- Cloudflare Worker code (deploy worker, point DNS)
- Live mainnet x402 test (real wallet + small USDC)
- Production Postgres setup
