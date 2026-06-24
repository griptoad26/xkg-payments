-- xkg-payments — Database schema
-- Engine: SQLite (dev) / PostgreSQL (prod) — all types below are portable
-- Author: xkg-dev, 2026-06-06

PRAGMA foreign_keys = ON;

-- ─── 1. customers ────────────────────────────────────────────────────────
-- A customer is an end-user of xkg-desktop. One-to-one with a Stripe
-- Customer object. We mirror the Stripe ID locally so we can join offline
-- (e.g. for analytics queries that don't need Stripe).
CREATE TABLE IF NOT EXISTS customers (
    id                  TEXT PRIMARY KEY,         -- local UUIDv4
    stripe_customer_id  TEXT UNIQUE NOT NULL,     -- cus_xxx
    email               TEXT NOT NULL,
    display_name        TEXT,
    metadata            TEXT,                     -- JSON blob (forwarded to/from Stripe)
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_customers_email ON customers(email);

-- ─── 2. products ─────────────────────────────────────────────────────────
-- A local cache of Stripe Products so we can show pricing tiers in the UI
-- without hitting Stripe on every page load. Refreshed by the `sync_products`
-- admin job.
CREATE TABLE IF NOT EXISTS products (
    id                  TEXT PRIMARY KEY,         -- local UUID
    stripe_product_id   TEXT UNIQUE NOT NULL,     -- prod_xxx
    name                TEXT NOT NULL,
    description         TEXT,
    tier                TEXT,                     -- free | pro | business | enterprise
    active              INTEGER NOT NULL DEFAULT 1,
    metadata            TEXT,                     -- JSON
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── 3. prices ───────────────────────────────────────────────────────────
-- Stripe Prices attached to Products. `unit_amount` is in the smallest
-- currency unit (cents for USD). `interval` is only set for recurring prices.
CREATE TABLE IF NOT EXISTS prices (
    id                  TEXT PRIMARY KEY,
    stripe_price_id     TEXT UNIQUE NOT NULL,     -- price_xxx
    product_id          TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    currency            TEXT NOT NULL DEFAULT 'usd',
    unit_amount         INTEGER NOT NULL,         -- cents
    interval            TEXT,                     -- day | week | month | year
    interval_count      INTEGER DEFAULT 1,
    trial_period_days   INTEGER,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_prices_product ON prices(product_id);

-- ─── 4. subscriptions ────────────────────────────────────────────────────
-- The local mirror of a Stripe Subscription. Updated by webhook events.
-- `status` mirrors Stripe's enum:
--   incomplete | incomplete_expired | trialing | active | past_due |
--   canceled | unpaid | paused
CREATE TABLE IF NOT EXISTS subscriptions (
    id                              TEXT PRIMARY KEY,
    stripe_subscription_id          TEXT UNIQUE NOT NULL,  -- sub_xxx
    customer_id                     TEXT NOT NULL REFERENCES customers(id),
    price_id                        TEXT NOT NULL REFERENCES prices(id),
    status                          TEXT NOT NULL,
    current_period_start            TEXT NOT NULL,
    current_period_end              TEXT NOT NULL,
    cancel_at_period_end            INTEGER NOT NULL DEFAULT 0,
    canceled_at                     TEXT,
    trial_start                     TEXT,
    trial_end                       TEXT,
    default_payment_method_id       TEXT,                   -- pm_xxx
    collection_method               TEXT DEFAULT 'charge_automatically',
    metadata                        TEXT,                   -- JSON
    created_at                      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_subs_customer ON subscriptions(customer_id);
CREATE INDEX idx_subs_status   ON subscriptions(status);

-- ─── 5. payment_intents ──────────────────────────────────────────────────
-- Local mirror of Stripe PaymentIntents — for one-off purchases or setup
-- flows that need a client_secret. Short-lived: we can prune after 30 days.
CREATE TABLE IF NOT EXISTS payment_intents (
    id                          TEXT PRIMARY KEY,
    stripe_payment_intent_id    TEXT UNIQUE NOT NULL,  -- pi_xxx
    customer_id                 TEXT REFERENCES customers(id),
    amount                      INTEGER NOT NULL,
    currency                    TEXT NOT NULL DEFAULT 'usd',
    status                      TEXT NOT NULL,
    client_secret               TEXT,                  -- only stored for active intents
    last_payment_error          TEXT,                  -- JSON
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pi_customer ON payment_intents(customer_id);

-- ─── 6. webhook_events ───────────────────────────────────────────────────
-- Append-only audit log of every webhook event received from Stripe.
-- `payload` is the full raw event JSON. `processed` flips to 1 once we've
-- applied the side effects (updates to subscriptions, customers, etc).
-- This table is the source of truth for replay and debugging.
CREATE TABLE IF NOT EXISTS webhook_events (
    id                  TEXT PRIMARY KEY,             -- local UUID
    stripe_event_id     TEXT UNIQUE NOT NULL,         -- evt_xxx
    type                TEXT NOT NULL,                -- e.g. invoice.paid
    api_version         TEXT,
    payload             TEXT NOT NULL,                -- full JSON
    processed           INTEGER NOT NULL DEFAULT 0,
    processed_at        TEXT,
    error               TEXT,                         -- error message if processing failed
    received_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_events_type       ON webhook_events(type);
CREATE INDEX idx_events_processed  ON webhook_events(processed);
CREATE INDEX idx_events_received   ON webhook_events(received_at);

-- ─── 7. invoices ─────────────────────────────────────────────────────────
-- Cached copy of Stripe Invoices. Used for the "Billing history" tab in
-- the desktop client. Source of truth is still Stripe; this is a mirror.
CREATE TABLE IF NOT EXISTS invoices (
    id                      TEXT PRIMARY KEY,
    stripe_invoice_id       TEXT UNIQUE NOT NULL,     -- in_xxx
    customer_id             TEXT NOT NULL REFERENCES customers(id),
    subscription_id         TEXT REFERENCES subscriptions(id),
    amount_due              INTEGER NOT NULL,
    amount_paid             INTEGER NOT NULL DEFAULT 0,
    currency                TEXT NOT NULL,
    status                  TEXT NOT NULL,            -- draft | open | paid | uncollectible | void
    hosted_invoice_url      TEXT,
    invoice_pdf             TEXT,
    period_start            TEXT,
    period_end              TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_invoices_customer ON invoices(customer_id);

-- ─── 8. audit_log ────────────────────────────────────────────────────────
-- Application-level audit trail. Every state-changing API call writes here
-- with (actor, action, before, after, ts). Useful for compliance / SOC2.
CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    actor       TEXT NOT NULL,                -- "user:<uuid>" or "system:webhook"
    action      TEXT NOT NULL,                -- e.g. "subscription.cancel"
    target      TEXT,                         -- e.g. "sub_xxx"
    before      TEXT,                         -- JSON
    after       TEXT,                         -- JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_actor ON audit_log(actor);
CREATE INDEX idx_audit_target ON audit_log(target);

-- ─── Views ───────────────────────────────────────────────────────────────

-- Active subscriptions with customer email (handy for the admin dashboard)
CREATE VIEW IF NOT EXISTS v_active_subscriptions AS
SELECT
    s.id                AS subscription_id,
    s.status,
    s.current_period_end,
    c.email             AS customer_email,
    c.stripe_customer_id,
    p.name              AS product_name,
    p.tier,
    pr.unit_amount,
    pr.currency,
    pr.interval
FROM subscriptions s
JOIN customers c  ON c.id = s.customer_id
JOIN prices    pr ON pr.id = s.price_id
JOIN products  p  ON p.id = pr.product_id
WHERE s.status IN ('active', 'trialing', 'past_due');

-- ─── 9. ledger_entries (AR-ready, processor-agnostic) ───────────────────
-- The single source of truth for every dollar in and dollar out, regardless
-- of which processor (Stripe, x402, LemonSqueezy, PayPal, Amazon Pay) handled
-- the payment. Designed so that an Accounts Receivable system can plug in
-- later as a query layer on top, NOT as a migration.
--
-- A row can represent:
--   1. A pending invoice  (status='pending', paid_at=NULL)
--   2. A completed sale   (status='succeeded', paid_at NOT NULL)
--   3. A refund           (status='refunded', refunded_at NOT NULL)
--   4. A void / chargeback (status='void')
--
-- Every entry has a `processor` so AR can report per-channel.
-- `external_id` is the processor's reference (e.g. pi_xxx, x402-tx-hash,
-- ls_order_xxx, paypal-order-xxx, amazon-order-xxx). Idempotent on this.
--
-- `due_at` lets AR track net-30 invoices. `customer_email` is denormalized
-- so AR doesn't need to join to `customers` for the most common reports.
CREATE TABLE IF NOT EXISTS ledger_entries (
    id                  TEXT PRIMARY KEY,                -- local UUID
    processor           TEXT NOT NULL,                   -- stripe | x402 | lemonsqueezy | paypal | amazonpay
    external_id         TEXT NOT NULL,                   -- processor's reference (idempotent key)
    status              TEXT NOT NULL,                   -- pending | succeeded | refunded | void | failed
    amount_cents        INTEGER NOT NULL,                -- gross amount in smallest currency unit
    currency            TEXT NOT NULL DEFAULT 'usd',     -- ISO 4217
    customer_id         TEXT REFERENCES customers(id),   -- NULL for guest checkout
    customer_email      TEXT,                            -- denormalized for AR reports
    invoice_number      TEXT,                            -- human-readable invoice id (INV-2026-0001)
    description         TEXT,                            -- what was purchased
    metadata_json       TEXT,                            -- JSON blob for processor-specific fields
    due_at              TEXT,                            -- when payment is expected (for AR aging)
    paid_at             TEXT,                            -- when payment succeeded
    refunded_at         TEXT,                            -- when refund was issued
    refund_reason       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ledger_processor   ON ledger_entries(processor);
CREATE INDEX idx_ledger_status      ON ledger_entries(status);
CREATE INDEX idx_ledger_customer    ON ledger_entries(customer_id);
CREATE INDEX idx_ledger_external    ON ledger_entries(processor, external_id);
CREATE INDEX idx_ledger_due_at      ON ledger_entries(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX idx_ledger_paid_at     ON ledger_entries(paid_at) WHERE paid_at IS NOT NULL;

-- ─── 10. invoice_sequences ────────────────────────────────────────────────
-- Generates the human-readable invoice_number field. One row per year.
-- AR reads/writes this; safe under concurrent INSERTs because we use the
-- counter atomically.
CREATE TABLE IF NOT EXISTS invoice_sequences (
    year        INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
);

-- ─── AR view: open receivables ────────────────────────────────────────────
-- A pending or partially-paid ledger entry. AR's main worklist.
-- (Stripe partial pays are modeled as status='pending' for now; we can
--  split into amount_paid later if real partials show up.)
CREATE VIEW IF NOT EXISTS v_open_receivables AS
SELECT
    id, processor, external_id,
    amount_cents, currency,
    customer_id, customer_email, invoice_number,
    description,
    due_at,
    CAST(JULIANDAY(due_at) - JULIANDAY('now') AS INTEGER) AS days_until_due,
    CAST(JULIANDAY('now') - JULIANDAY(due_at) AS INTEGER) AS days_overdue
FROM ledger_entries
WHERE status = 'pending'
  AND due_at IS NOT NULL
ORDER BY due_at ASC;

-- ─── AR view: settled ledger (last 90 days) ──────────────────────────────
CREATE VIEW IF NOT EXISTS v_recent_settlements AS
SELECT
    id, processor, external_id,
    amount_cents, currency,
    customer_email, invoice_number, description,
    paid_at, refunded_at
FROM ledger_entries
WHERE paid_at IS NOT NULL
  AND paid_at > datetime('now', '-90 days')
ORDER BY paid_at DESC;
