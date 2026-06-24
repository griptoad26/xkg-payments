# xkg-payments — Setup Guide

## 1. Get Stripe test keys

1. Sign in to https://dashboard.stripe.com
2. **Switch to "Test mode"** (toggle in the top-right). The dashboard
   appearance changes — orange is live, purple is test.
3. Go to **Developers → API keys**
4. Reveal the **Secret key** (`sk_test_...`) and copy the **Publishable
   key** (`pk_test_...`).
5. Save them somewhere safe (e.g. password manager).

## 2. Set up the webhook

1. In the same Stripe dashboard, go to **Developers → Webhooks**
2. Click **Add endpoint**
3. **Endpoint URL:** `http://127.0.0.1:8765/v1/webhooks/stripe` (use
   ngrok / tailscale funnel if xkg-payments is remote)
4. **Description:** `xkg-payments local dev`
5. **Listen for:** select these events:
   - `customer.created`, `customer.updated`, `customer.deleted`
   - `customer.subscription.created`, `customer.subscription.updated`,
     `customer.subscription.deleted`
   - `invoice.paid`, `invoice.payment_failed`, `invoice.finalized`
6. Click **Add endpoint**
7. Click on the new endpoint → reveal **Signing secret** (`whsec_...`)

## 3. Configure xkg-payments

```bash
cd /home/x2/.openclaw/workspace/xkg-payments
cp .env.example .env
$EDITOR .env    # paste the three keys from steps 1 + 2
```

Then either `export` the vars or use `direnv` / `dotenv`.

## 4. Run

```bash
# Install once
pip install -r requirements.txt

# Start the service
python3 -m uvicorn service.main:app --host 127.0.0.1 --port 8765

# In another terminal: smoke tests
python3 tests/test_service.py
```

## 5. Try a real purchase

1. Open the Stripe dashboard → **Payments → Create payment** (or use a
   pre-made test card: `4242 4242 4242 4242`, any future expiry, any CVC)
2. Use the OpenAPI docs at http://127.0.0.1:8765/docs to:
   - `POST /v1/customers` → save `id`
   - `POST /v1/subscriptions` with that `id` and a `price_id` from
     `/v1/products`
3. Watch the webhook fire in the service log
4. Verify the row appears in your local DB:
   ```bash
   sqlite3 xkg-payments.db 'SELECT * FROM subscriptions'
   ```

## 6. Build the Tauri desktop client

The desktop client is a thin shell. To build:

```bash
cd desktop
# (requires Node 22+, Rust 1.77+, Tauri prerequisites)
npm install
npm install -g @tauri-apps/cli@2
cargo tauri dev          # dev build
cargo tauri build        # release build
```

`cargo tauri build` produces:
- Linux: `.deb`, `.rpm`, `.AppImage`
- macOS: `.app`, `.dmg`
- Windows: `.msi`, `.nsis`

The release binary is < 10 MB (no bundled browser, no embedded DB).

## 7. Going to production

Before flipping to `STRIPE_TEST_MODE=false`:

- [ ] Replace `devtoken-change-me` in `API_BEARER_TOKENS` with a
      cryptographically random string (or switch to a proper auth scheme
      — JWTs from your existing identity provider).
- [ ] Switch `DATABASE_URL` from `sqlite:///./...` to a real Postgres
      connection string.
- [ ] Run behind HTTPS (Caddy / nginx / cloud LB).
- [ ] Update the Stripe webhook URL to your production endpoint
      (`https://payments.xkg.agency/v1/webhooks/stripe`).
- [ ] Set up log shipping + monitoring (Prometheus `/metrics` is on the
      roadmap).
- [ ] Review the audit log retention policy.
