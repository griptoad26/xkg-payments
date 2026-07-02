# Stripe Live Mode — Go-Live Playbook

_Owner: stripe-agent. Last verified: 2026-07-01._

This is the step-by-step for taking `xkg-payments` from test mode to live
mode on Stripe. **Live mode = real cards, real money, no undo.** Read every
step before doing it. The whole process takes ~15 minutes if you have the
keys in hand; otherwise it's blocked on Stripe dashboard access.

## TL;DR

```bash
# 1. Stage live creds in env (do this first; service still uses test keys)
export STRIPE_LIVE_SK=sk_live_…
export STRIPE_LIVE_PK=pk_live_…
export STRIPE_LIVE_WEBHOOK_SECRET=whsec_…
export X_ADMIN_TOKEN=$(openssl rand -hex 32)

# 2. Dry-run the readiness check
curl -s -H "X-Admin-Token: $X_ADMIN_TOKEN" \
  https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/admin/stripe-readiness | jq

# → expect: "ready_for_live": true, "blockers": []

# 3. Flip active keys + flag
export STRIPE_SECRET_KEY=$STRIPE_LIVE_SK
export STRIPE_PUBLISHABLE_KEY=$STRIPE_LIVE_PK
export STRIPE_WEBHOOK_SECRET=$STRIPE_LIVE_WEBHOOK_SECRET
export STRIPE_TEST_MODE=false
systemctl restart xkg-payments   # or however you run it

# 4. Verify
curl -s https://x2-nuc.tailb0d54b.ts.net/api/pay/health | jq
# → expect: "test_mode": false

# 5. $0.50 smoke test (see below)

# 6. Tell Stripe dashboard to send webhooks to:
#    https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/webhooks/stripe
```

## What needs to change to go live

| Var | Test value | Live value | Notes |
|---|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` | `sk_live_…` | Dashboard → Developers → API keys → Reveal live key |
| `STRIPE_PUBLISHABLE_KEY` | `pk_test_…` | `pk_live_…` | Same place, "publishable" tab |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` (test mode endpoint) | NEW `whsec_…` (live mode endpoint) | Must create a new endpoint in **live** mode in the dashboard; the test-mode secret won't work |
| `STRIPE_TEST_MODE` | `true` | `false` | Single boolean gates everything |
| `API_BEARER_TOKENS` | `devtoken-change-me` | rotated | Rotate before going live |
| `X_ADMIN_TOKEN` | unset | random 32 bytes | Enables `/v1/admin/stripe-readiness` |

**Critical gotchas:**

1. **Webhook secrets are mode-specific.** A `whsec_…` you copy from the
   test-mode endpoint will be rejected by live-mode events (and vice
   versa). You must create the webhook endpoint separately in live mode.
2. **Webhook URLs are mode-specific too.** Point live-mode webhooks at
   the same URL (`https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/webhooks/stripe`),
   but make sure the Stripe dashboard is configuring the *live* endpoint,
   not the test one.
3. **`allowed_redirect_urls` on Checkout Sessions.** If you ever switch
   from PaymentIntents to hosted Checkout, you'll need to whitelist your
   success/cancel URLs in the dashboard. We're currently on PaymentIntents
   so this doesn't apply yet.
4. **The Stripe Python SDK picks up `STRIPE_SECRET_KEY` at import time.**
   You MUST restart the service after changing env vars. A `kill -HUP`
   won't do it.

## Dry-run readiness check (do this first)

Before flipping anything, confirm you have everything in place:

```bash
# 1. Set ONLY the staging vars (the service is still in test mode)
export STRIPE_LIVE_SK=sk_live_…   # from Stripe live dashboard
export STRIPE_LIVE_PK=pk_live_…
export STRIPE_LIVE_WEBHOOK_SECRET=whsec_…
export X_ADMIN_TOKEN=$(openssl rand -hex 32)

# 2. Restart the service so it picks up X_ADMIN_TOKEN (STRIPE_LIVE_* are
#    only read by the readiness handler; they don't affect SDK behavior)
systemctl restart xkg-payments

# 3. Hit the readiness endpoint
curl -s -H "X-Admin-Token: $X_ADMIN_TOKEN" \
  https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/admin/stripe-readiness | jq
```

Expected response when fully ready:

```json
{
  "test_mode": true,
  "has_live_pk": true,
  "has_live_sk": true,
  "has_live_webhook_secret": true,
  "has_webhook_secret": true,
  "active_sk_prefix": "sk_test_...",
  "active_pk_prefix": "pk_test_...",
  "api_version": "2025-03-31.basil",
  "ready_for_live": false,
  "blockers": [
    "STRIPE_TEST_MODE is still true — set STRIPE_TEST_MODE=false to go live"
  ]
}
```

When you've also set `STRIPE_TEST_MODE=false` (and the service is restarted
with the live `STRIPE_SECRET_KEY`), `ready_for_live` flips to `true` and
`blockers` is `[]`. That's your green light.

The endpoint never calls Stripe's API — it only reads env vars. Safe to hit
during a deploy.

## Smoke test: $0.50 product in live mode

The cheapest possible end-to-end test:

```bash
curl -s -X POST https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/checkout \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_BEARER_TOKENS" \
  -d '{
    "method": "stripe",
    "amount_cents": 50,
    "currency": "usd",
    "description": "LIVE MODE SMOKE TEST — please ignore",
    "customer_email": "qa@example.com",
    "idempotency_key": "live-smoke-2026-07-01"
  }' | jq
```

Expected response:

```json
{
  "id": "<local-uuid>",
  "method": "stripe",
  "status": "pending",
  "amount_cents": 50,
  "currency": "usd",
  "processor_payload": {
    "stripe_client_secret": "pi_…_secret_…",
    "stripe_payment_intent_id": "pi_…"
  },
  "ledger_entry_id": "<local-uuid>"
}
```

Note the `pi_…` prefix — in live mode, the PaymentIntent ID will start with
`pi_` but Stripe *does not* encode the mode in the ID. To confirm you're
hitting live, the easiest check is to look at the dashboard at
https://dashboard.stripe.com/test/payments (test) vs
https://dashboard.stripe.com/payments (live). After the smoke test, you
should see the $0.50 charge on the **live** dashboard within 5 seconds.

Then complete the charge with Stripe's test card in live mode. Yes, in live
mode, you can still use Stripe's test card numbers (4242 4242 4242 4242)
for the first few real charges — Stripe knows you're testing. After you've
done a couple, they may require real cards. The $0.50 charge will appear as
a real charge in your live dashboard.

**Refund it immediately.** Hit the Stripe dashboard → Payments → click
the $0.50 charge → Refund → full refund. Don't leave a $0.50 charge
sitting there looking like production.

## Verifying webhooks fire

1. Stripe dashboard → Developers → Webhooks → add endpoint:
   - URL: `https://x2-nuc.tailb0d54b.ts.net/api/pay/v1/webhooks/stripe`
   - Events to send: at minimum `payment_intent.succeeded` and
     `checkout.session.completed` (we listen to more — see
     `service/main.py:_apply_event`)
   - Mode: **Live** (not Test)
2. Click "Send test event" → choose `payment_intent.succeeded`.
3. Expected response: `200 OK`.
4. Check the xkg-payments logs:
   ```bash
   tail -f /tmp/xkg-payments.log
   # expect: "xkg-payments ready: … test_mode=False …"
   # then:    "Webhook signature failed" if the test event had a bad sig
   # OR:     "received: True" if Stripe's test signature was valid
   ```
5. Hit `/v1/ledger/<entry_id>` and confirm `status: "succeeded"` and
   `paid_at` is set.

If you get a 400 with `bad_signature`, the webhook secret in env doesn't
match the dashboard's. Re-copy from the dashboard and restart the service.

## Rollback plan

If something goes wrong in the first 24 hours, flipping back to test mode
is the inverse:

```bash
# Restore test keys
export STRIPE_SECRET_KEY=sk_test_…   # from test dashboard
export STRIPE_PUBLISHABLE_KEY=pk_test_…
export STRIPE_WEBHOOK_SECRET=whsec_…  # the *test* mode webhook secret
export STRIPE_TEST_MODE=true
systemctl restart xkg-payments

# Verify
curl -s https://x2-nuc.tailb0d54b.ts.net/api/pay/health | jq
# → expect: "test_mode": true
```

**Important:** any in-flight PaymentIntents from the live window will
continue to complete on Stripe's side. To void/refund them, go to the
live dashboard → Payments → filter by date → click each → Refund. xkg-payments
in test mode will not see those webhook events arrive (they go to the live
webhook endpoint, which still points at the live service, but the service
now expects test signatures → rejects → 400 in the logs, which is fine).

If a customer actually paid real money during the bad window: refund them
in the live dashboard. The ledger row will still show `pending` in
xkg-payments, but the customer got their money back. Add a manual `paid_at`
patch in the DB if you need to reconcile.

## Post-go-live checklist

- [ ] Update `MEMORY.md` to note live mode is on
- [ ] Confirm Cloudflare Access / tailnet funnel is still pointing at the
      service (it should be — we didn't change routing)
- [ ] Add live-mode webhook URL to Stripe dashboard
- [ ] Confirm `/v1/webhooks/stripe` returns 200 on a real event
- [ ] Set up Stripe email alerts for failed payments (Dashboard → Settings →
      Emails → add `qa@example.com` to "Charge failure" and "Dispute created")
- [ ] Rotate `API_BEARER_TOKENS` if you haven't already (any token in any
      client's env file is now a live-money credential)
- [ ] Move the SQLite DB to Postgres if you haven't (SQLite is fine for
      single-host low-write; live mode has more writes)
- [ ] Set up a daily `crontab` that calls `/v1/admin/stripe-readiness` and
      posts the result to Discord #ops so drift is caught early

## Where to look in code

- `service/config.py` — `Settings.live_readiness()` is the source of truth
  for what "ready" means. Update it if the go-live checklist changes.
- `service/main.py` — `GET /v1/admin/stripe-readiness` (gated by
  `_require_admin`).
- `service/stripe_client.py` — all Stripe calls; reads
  `settings.stripe_secret_key` at module import time.
- `service/checkout.py` — the `/v1/checkout` dispatcher that creates
  PaymentIntents.
- `.env.example` — documents every var, including the staging
  `STRIPE_LIVE_*` trio.

## Contact

- Stripe support: https://support.stripe.com (chat is fastest)
- Internal: ping `stripe-agent` via the OCMI hub