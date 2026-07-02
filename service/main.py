"""xkg-payments — FastAPI service entrypoint.

Run with:
    uvicorn service.main:app --host 127.0.0.1 --port 8765

Endpoints:
    GET  /health                      — liveness probe
    GET  /v1/products                 — list cached Stripe products
    POST /v1/customers                — create a customer
    GET  /v1/customers/{id}           — fetch customer
    POST /v1/subscriptions            — create a subscription
    GET  /v1/subscriptions/{id}       — fetch subscription
    POST /v1/subscriptions/{id}/cancel — cancel (at period end or immediately)
    POST /v1/webhooks/stripe          — Stripe webhook receiver
"""
from __future__ import annotations

import json
import logging
import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from .config import settings
from . import stripe_client
from .checkout import router as checkout_router
# When run as `uvicorn service.main:app`, service/ is the top-level package,
# so use a plain import for the sibling db/ directory after adding cwd.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import models  # type: ignore[no-redef]

log = logging.getLogger("xkg-payments")
logging.basicConfig(level=settings.log_level)


# ── Auth ──────────────────────────────────────────────────────────────────

def require_bearer(authorization: Optional[str] = Header(None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token not in settings.api_bearer_tokens:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


# ── App + lifecycle ───────────────────────────────────────────────────────

app = FastAPI(title="xkg-payments", version="0.2.0")
# Auth is enforced per-route via Depends(require_bearer) so the health
# endpoint stays public and the webhook can verify its own signature.
from . import auth
app.include_router(checkout_router)  # /v1/checkout, /v1/x402/settle, /v1/ledger, /v1/ar/*
app.include_router(auth.router)        # /v1/auth/{register,login,logout,me}

@app.on_event("startup")
def _startup() -> None:
    models.init_db()
    log.info("xkg-payments ready: %s", settings.safe_summary())
    # site_users + site_sessions (auth)
    from .auth import _ensure_tables as _auth_et
    _auth_et()


@app.get("/health", dependencies=[])
def health() -> dict:
    return {"status": "ok", "test_mode": settings.stripe_test_mode, "version": "0.1.0"}


# ── Admin (readiness, dry-run checks) ─────────────────────────────────────
# Gated by X-Admin-Token (env var). Header: `X-Admin-Token: <value>`.
# Returns 403 if X_ADMIN_TOKEN is unset OR if the header doesn't match.
# Returns 200 with the readiness report otherwise.

def _require_admin(x_admin_token: Optional[str] = Header(None)) -> None:
    expected = settings.admin_token
    if not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin endpoints disabled (X_ADMIN_TOKEN not set)")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid X-Admin-Token")


@app.get("/v1/admin/stripe-readiness", dependencies=[Depends(_require_admin)])
def stripe_readiness() -> dict:
    """Dry-run check: would xkg-payments be ready to flip to live Stripe?

    Inspects env vars only — does NOT change any state, does NOT call the
    Stripe API, does NOT touch the database. Safe to hit anytime.
    """
    return settings.live_readiness()


# ── Schemas ───────────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class CustomerOut(BaseModel):
    id: str
    email: str
    name: Optional[str]
    stripe_customer_id: str

    @classmethod
    def from_row(cls, c: models.Customer) -> "CustomerOut":
        return cls(id=c.id, email=c.email, name=c.display_name,
                   stripe_customer_id=c.stripe_customer_id)


class SubscriptionCreate(BaseModel):
    customer_id: str           # local UUID
    price_id: str              # local UUID
    trial_days: Optional[int] = None
    metadata: dict = Field(default_factory=dict)


class SubscriptionOut(BaseModel):
    id: str
    status: str
    current_period_end: datetime
    cancel_at_period_end: bool
    stripe_subscription_id: str

    @classmethod
    def from_row(cls, s: models.Subscription) -> "SubscriptionOut":
        return cls(
            id=s.id, status=s.status, current_period_end=s.current_period_end,
            cancel_at_period_end=s.cancel_at_period_end,
            stripe_subscription_id=s.stripe_subscription_id,
        )


class CancelRequest(BaseModel):
    at_period_end: bool = True


# ── Products ──────────────────────────────────────────────────────────────

@app.get("/v1/products", dependencies=[Depends(require_bearer)])
def list_products(db: Session = Depends(models.get_session)) -> dict:
    rows = db.query(models.Product).filter(models.Product.active == True).all()
    return {
        "data": [
            {
                "id": p.id, "name": p.name, "tier": p.tier, "description": p.description,
                "stripe_product_id": p.stripe_product_id,
                "prices": [
                    {
                        "id": pr.id, "currency": pr.currency, "unit_amount": pr.unit_amount,
                        "interval": pr.interval, "interval_count": pr.interval_count,
                        "trial_period_days": pr.trial_period_days,
                        "stripe_price_id": pr.stripe_price_id,
                    }
                    for pr in p.prices if pr.active
                ],
            } for p in rows
        ]
    }


# ── Customers ─────────────────────────────────────────────────────────────

@app.post("/v1/customers", response_model=CustomerOut, status_code=201, dependencies=[Depends(require_bearer)])
def create_customer(body: CustomerCreate, db: Session = Depends(models.get_session)) -> CustomerOut:
    try:
        sc = stripe_client.create_customer(email=body.email, name=body.name, metadata=body.metadata)
    except stripe_client.StripeError as e:
        raise HTTPException(e.status_code, e.message)
    row = models.Customer(
        stripe_customer_id=sc["id"], email=sc["email"], display_name=sc.get("name"),
        metadata_json=json.dumps(sc.get("metadata") or {}),
    )
    db.add(row); db.commit(); db.refresh(row)
    db.add(models.AuditLog(actor="user:api", action="customer.create", target=row.stripe_customer_id,
                            after=json.dumps({"email": body.email})))
    db.commit()
    return CustomerOut.from_row(row)


@app.get("/v1/customers/{local_id}", response_model=CustomerOut, dependencies=[Depends(require_bearer)])
def get_customer(local_id: str, db: Session = Depends(models.get_session)) -> CustomerOut:
    row = db.get(models.Customer, local_id)
    if not row: raise HTTPException(404, "Customer not found")
    return CustomerOut.from_row(row)


# ── Subscriptions ─────────────────────────────────────────────────────────

@app.post("/v1/subscriptions", response_model=SubscriptionOut, status_code=201, dependencies=[Depends(require_bearer)])
def create_subscription(body: SubscriptionCreate, db: Session = Depends(models.get_session)) -> SubscriptionOut:
    customer = db.get(models.Customer, body.customer_id)
    price    = db.get(models.Price, body.price_id)
    if not customer: raise HTTPException(404, "Customer not found")
    if not price:    raise HTTPException(404, "Price not found")
    try:
        ss = stripe_client.create_subscription(
            customer_id=customer.stripe_customer_id, price_id=price.stripe_price_id,
            trial_days=body.trial_days, metadata=body.metadata,
        )
    except stripe_client.StripeError as e:
        raise HTTPException(e.status_code, e.message)

    row = models.Subscription(
        stripe_subscription_id=ss["id"],
        customer_id=customer.id, price_id=price.id,
        status=ss["status"],
        current_period_start=(
            datetime.fromtimestamp(ss["current_period_start"], tz=timezone.utc)
            if isinstance(ss["current_period_start"], int)
            else datetime.fromisoformat(ss["current_period_start"].replace("Z", "+00:00"))
        ),
        current_period_end=(
            datetime.fromtimestamp(ss["current_period_end"], tz=timezone.utc)
            if isinstance(ss["current_period_end"], int)
            else datetime.fromisoformat(ss["current_period_end"].replace("Z", "+00:00"))
        ),
        cancel_at_period_end=bool(ss.get("cancel_at_period_end", False)),
        trial_start=ss.get("trial_start"), trial_end=ss.get("trial_end"),
        default_payment_method_id=ss.get("default_payment_method"),
        collection_method=ss.get("collection_method", "charge_automatically"),
        metadata_json=json.dumps(ss.get("metadata") or {}),
    )
    db.add(row); db.commit(); db.refresh(row)
    db.add(models.AuditLog(actor="user:api", action="subscription.create", target=ss["id"]))
    db.commit()
    return SubscriptionOut.from_row(row)


@app.get("/v1/subscriptions/{local_id}", response_model=SubscriptionOut, dependencies=[Depends(require_bearer)])
def get_subscription(local_id: str, db: Session = Depends(models.get_session)) -> SubscriptionOut:
    row = db.get(models.Subscription, local_id)
    if not row: raise HTTPException(404, "Subscription not found")
    return SubscriptionOut.from_row(row)


@app.post("/v1/subscriptions/{local_id}/cancel", response_model=SubscriptionOut, dependencies=[Depends(require_bearer)])
def cancel_subscription(local_id: str, body: CancelRequest, db: Session = Depends(models.get_session)) -> SubscriptionOut:
    row = db.get(models.Subscription, local_id)
    if not row: raise HTTPException(404, "Subscription not found")
    try:
        ss = stripe_client.cancel_subscription(row.stripe_subscription_id, at_period_end=body.at_period_end)
    except stripe_client.StripeError as e:
        raise HTTPException(e.status_code, e.message)
    row.status = ss["status"]
    row.cancel_at_period_end = bool(ss.get("cancel_at_period_end", False))
    if ss.get("canceled_at"):
        row.canceled_at = ss["canceled_at"]
    db.commit(); db.refresh(row)
    db.add(models.AuditLog(actor="user:api", action="subscription.cancel", target=row.stripe_subscription_id))
    db.commit()
    return SubscriptionOut.from_row(row)


# ── Webhooks ──────────────────────────────────────────────────────────────

# IMPORTANT: webhook endpoint must read raw body for signature verification,
# so we exclude it from the bearer-token dependency and the global JSON parsing.
@app.post("/v1/webhooks/stripe", dependencies=[])
async def stripe_webhook(request: Request, db: Session = Depends(models.get_session)) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_client.verify_webhook(payload, sig)
    except stripe_client.StripeError as e:
        log.warning("Webhook signature failed: %s", e.message)
        return JSONResponse({"error": e.message}, status_code=e.status_code)

    # Idempotent insert: ignore duplicate event IDs.
    existing = db.query(models.WebhookEvent).filter_by(stripe_event_id=event["id"]).one_or_none()
    if existing:
        return JSONResponse({"received": True, "duplicate": True, "id": event["id"]})

    row = models.WebhookEvent(
        stripe_event_id=event["id"], type=event["type"],
        api_version=event.get("api_version"), payload=json.dumps(event),
    )
    db.add(row)
    try:
        _apply_event(event, db)
        row.processed = True
        row.processed_at = datetime.utcnow()
    except Exception as exc:  # noqa: BLE001
        log.exception("Webhook processing failed for %s", event["id"])
        row.error = f"{type(exc).__name__}: {exc}"
    db.commit()
    return JSONResponse({"received": True, "id": event["id"], "type": event["type"]})


def _apply_event(event: dict, db: Session) -> None:
    """Idempotent side-effect application. Extend as more event types are needed."""
    et = event["type"]
    obj = event["data"]["object"]

    if et in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        _upsert_subscription(obj, db)
    elif et == "customer.created" or et == "customer.updated":
        _upsert_customer(obj, db)
    elif et == "invoice.paid" or et == "invoice.payment_failed" or et == "invoice.finalized":
        _upsert_invoice(obj, db)
    else:
        log.info("Ignoring unhandled webhook type: %s", et)


def _upsert_customer(obj: dict, db: Session) -> None:
    row = db.query(models.Customer).filter_by(stripe_customer_id=obj["id"]).one_or_none()
    if row is None:
        row = models.Customer(stripe_customer_id=obj["id"], email=obj.get("email") or "")
    row.email = obj.get("email") or row.email
    row.display_name = obj.get("name")
    row.metadata_json = json.dumps(obj.get("metadata") or {})
    db.add(row); db.flush()


def _upsert_subscription(obj: dict, db: Session) -> None:
    cust = db.query(models.Customer).filter_by(stripe_customer_id=obj["customer"]).one_or_none()
    if not cust:
        log.warning("Subscription %s for unknown customer %s — skipping", obj["id"], obj["customer"])
        return
    # Find the price by stripe_price_id; create a placeholder if missing.
    item = (obj.get("items", {}).get("data") or [{}])[0]
    stripe_price_id = item.get("price", {}).get("id")
    price = db.query(models.Price).filter_by(stripe_price_id=stripe_price_id).one_or_none() if stripe_price_id else None
    if price is None:
        # Use a placeholder if we don't have a product match — webhook will catch up.
        price = models.Price(
            stripe_price_id=stripe_price_id or "unknown",
            product_id=(db.query(models.Product).first().id if db.query(models.Product).first() else "00000000-0000-0000-0000-000000000000"),
            unit_amount=0, currency=(item.get("price", {}).get("currency") or "usd"),
            interval=item.get("price", {}).get("recurring", {}).get("interval") if item.get("price", {}).get("recurring") else None,
        )
        db.add(price); db.flush()

    row = db.query(models.Subscription).filter_by(stripe_subscription_id=obj["id"]).one_or_none()
    if row is None:
        row = models.Subscription(stripe_subscription_id=obj["id"], customer_id=cust.id, price_id=price.id,
                                   status=obj["status"], current_period_start=datetime.utcnow(),
                                   current_period_end=datetime.utcnow())
    row.status = obj["status"]
    row.cancel_at_period_end = bool(obj.get("cancel_at_period_end", False))
    if obj.get("current_period_start"):
        row.current_period_start = datetime.fromtimestamp(obj["current_period_start"], tz=timezone.utc)
    if obj.get("current_period_end"):
        row.current_period_end = datetime.fromtimestamp(obj["current_period_end"], tz=timezone.utc)
    db.add(row); db.flush()


def _upsert_invoice(obj: dict, db: Session) -> None:
    cust = db.query(models.Customer).filter_by(stripe_customer_id=obj["customer"]).one_or_none()
    if not cust: return
    row = db.query(models.Invoice).filter_by(stripe_invoice_id=obj["id"]).one_or_none()
    if row is None:
        row = models.Invoice(stripe_invoice_id=obj["id"], customer_id=cust.id,
                              amount_due=obj.get("amount_due", 0), currency=obj.get("currency", "usd"),
                              status=obj.get("status", "draft"))
    row.amount_due = obj.get("amount_due", row.amount_due)
    row.amount_paid = obj.get("amount_paid", row.amount_paid)
    row.status = obj.get("status", row.status)
    row.hosted_invoice_url = obj.get("hosted_invoice_url")
    row.invoice_pdf = obj.get("invoice_pdf")
    db.add(row); db.flush()
