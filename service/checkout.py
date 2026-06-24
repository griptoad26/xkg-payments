"""Unified checkout dispatcher.

One endpoint (`/v1/checkout`) accepts a payment request with a `method`
field and dispatches to the right processor:

    method=stripe        → Stripe PaymentIntent (card)
    method=x402          → x402 challenge (USDC on Base)
    method=lemonsqueezy  → LemonSqueezy checkout URL  (P3, not yet)
    method=paypal        → PayPal Orders v2            (P4, not yet)
    method=amazonpay     → Amazon Pay v2 checkout      (P5, not yet)

Every successful (or pending) charge lands as a row in `ledger_entries`,
which is the AR-ready source of truth.

The processor-specific implementations live in their own modules
(stripe_client.py, x402.py, lemonsqueezy.py, paypal.py, amazonpay.py).
This module is the router + the ledger writer.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, conint
from sqlalchemy.orm import Session

from .config import settings
from . import stripe_client, x402
from db import models  # type: ignore[no-redef]

log = logging.getLogger("xkg-payments.checkout")

router = APIRouter()


# ── Public schemas ─────────────────────────────────────────────────────────

Method = Literal["stripe", "x402", "lemonsqueezy", "paypal", "amazonpay"]


class CheckoutRequest(BaseModel):
    method: Method
    amount_cents: conint(gt=0, le=10_000_00)  # $0.01 – $10,000 hard cap for v1
    currency: str = Field(default="usd", min_length=3, max_length=3)
    description: str = Field(..., min_length=1, max_length=500)
    customer_email: Optional[EmailStr] = None
    customer_name: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    # Processor-specific pass-through
    metadata: dict = Field(default_factory=dict)
    # For x402: which plan key (pro/api/bundle/...) for pricing lookup
    plan: Optional[str] = None
    # Idempotency key — if you retry with the same key, you get the same
    # result back without creating a new charge. Caller-supplied; we
    # forward it as Stripe's idempotency_key and as a metadata hint for x402.
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class CheckoutResponse(BaseModel):
    id: str                                       # ledger entry id
    invoice_number: Optional[str] = None
    method: str
    status: str                                   # pending | succeeded
    amount_cents: int
    currency: str
    # Processor-specific handoff (different shape per method)
    processor_payload: dict = Field(default_factory=dict)
    ledger_entry_id: str
    expires_at: Optional[datetime] = None


# ── Helpers ────────────────────────────────────────────────────────────────

def _allocate_ledger_entry(
    db: Session,
    *,
    processor: str,
    external_id: str,
    amount_cents: int,
    currency: str,
    status_str: str,
    customer_email: Optional[str],
    description: str,
    metadata: dict,
    paid_at: Optional[datetime] = None,
    due_at: Optional[datetime] = None,
) -> models.LedgerEntry:
    """Write a ledger row. Idempotent on (processor, external_id)."""
    existing = (
        db.query(models.LedgerEntry)
        .filter_by(processor=processor, external_id=external_id)
        .one_or_none()
    )
    if existing:
        log.info("ledger idempotent hit: %s/%s -> %s", processor, external_id, existing.id)
        return existing

    entry = models.LedgerEntry(
        processor=processor,
        external_id=external_id,
        status=status_str,
        amount_cents=amount_cents,
        currency=currency.lower(),
        customer_email=customer_email,
        description=description,
        metadata_json=json.dumps(metadata or {}),
        paid_at=paid_at,
        due_at=due_at,
    )
    db.add(entry)
    db.flush()
    # Allocate invoice number only when we have a paid event (pending entries
    # get a number when they succeed).
    if status_str == "succeeded" and not entry.invoice_number:
        entry.invoice_number = models.next_invoice_number(db, datetime.now(timezone.utc).year)
    db.flush()
    return entry


def _mark_paid(db: Session, entry: models.LedgerEntry, paid_at: Optional[datetime] = None) -> None:
    entry.status = "succeeded"
    entry.paid_at = paid_at or datetime.now(timezone.utc)
    if not entry.invoice_number:
        entry.invoice_number = models.next_invoice_number(db, entry.paid_at.year)
    db.flush()


# ── Dispatcher ─────────────────────────────────────────────────────────────

@router.post("/v1/checkout", response_model=CheckoutResponse, status_code=201)
def checkout(body: CheckoutRequest, db: Session = Depends(models.get_session)) -> CheckoutResponse:
    """Create a payment intent via the chosen processor."""
    try:
        if body.method == "stripe":
            return _checkout_stripe(body, db)
        elif body.method == "x402":
            return _checkout_x402(body, db)
        elif body.method == "lemonsqueezy":
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "LemonSqueezy: pending P3")
        elif body.method == "paypal":
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "PayPal: pending P4")
        elif body.method == "amazonpay":
            raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Amazon Pay: pending P5")
        else:
            raise HTTPException(400, f"Unknown method: {body.method}")
    except stripe_client.StripeError as e:
        log.warning("Stripe error in checkout: %s", e.message)
        raise HTTPException(e.status_code, e.message)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("checkout failed for method=%s", body.method)
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


def _checkout_stripe(body: CheckoutRequest, db: Session) -> CheckoutResponse:
    # Idempotency at the ledger level: if (processor, external_id) already
    # exists, return the prior entry. We use body.idempotency_key as the
    # external_id if provided, otherwise the Stripe PaymentIntent id.
    if body.idempotency_key:
        existing = (
            db.query(models.LedgerEntry)
            .filter_by(processor="stripe", external_id=body.idempotency_key)
            .one_or_none()
        )
        if existing:
            return CheckoutResponse(
                id=existing.id, invoice_number=existing.invoice_number,
                method="stripe", status=existing.status,
                amount_cents=existing.amount_cents, currency=existing.currency,
                processor_payload=json.loads(existing.metadata_json or "{}"),
                ledger_entry_id=existing.id,
            )

    intent = stripe_client.create_payment_intent(
        amount_cents=body.amount_cents,
        currency=body.currency,
        description=body.description,
        receipt_email=body.customer_email,
        metadata={**(body.metadata or {}), "idempotency_key": body.idempotency_key}
                 if body.idempotency_key else body.metadata,
        idempotency_key=body.idempotency_key,
    )
    entry = _allocate_ledger_entry(
        db,
        processor="stripe",
        external_id=intent["id"],
        amount_cents=body.amount_cents,
        currency=body.currency,
        status_str=intent["status"],  # typically "requires_payment_method"
        customer_email=body.customer_email,
        description=body.description,
        metadata={"stripe_client_secret": intent.get("client_secret"), **(body.metadata or {})},
    )
    db.commit()
    return CheckoutResponse(
        id=entry.id,
        invoice_number=entry.invoice_number,
        method="stripe",
        status=entry.status,
        amount_cents=body.amount_cents,
        currency=body.currency.lower(),
        processor_payload={
            "stripe_client_secret": intent.get("client_secret"),
            "stripe_payment_intent_id": intent["id"],
        },
        ledger_entry_id=entry.id,
    )


def _checkout_x402(body: CheckoutRequest, db: Session) -> CheckoutResponse:
    plan = body.plan or "custom"
    challenge = x402.build_challenge(plan, {
        "amount": body.amount_cents,
        "name": body.description,
    })
    payment_id = challenge["payment_id"]
    expires = datetime.fromisoformat(challenge["headers"]["Payment-Required"]
                                       .replace("'", '"')  # if JSON-as-string is parsed safely later
                                       .split("\"expires\":\"")[1].split("\"")[0]) \
        if False else datetime.now(timezone.utc) + timedelta(hours=1)

    entry = _allocate_ledger_entry(
        db,
        processor="x402",
        external_id=payment_id,
        amount_cents=body.amount_cents,
        currency=body.currency,
        status_str="pending",
        customer_email=body.customer_email,
        description=body.description,
        metadata={"challenge": challenge, "plan": plan, **(body.metadata or {})},
        due_at=expires,
    )
    db.commit()
    return CheckoutResponse(
        id=entry.id,
        invoice_number=entry.invoice_number,
        method="x402",
        status="pending",
        amount_cents=body.amount_cents,
        currency=body.currency.lower(),
        processor_payload=challenge,
        ledger_entry_id=entry.id,
        expires_at=expires,
    )


# ── Settle (x402) ──────────────────────────────────────────────────────────

class X402SettleRequest(BaseModel):
    payment_id: str
    tx_hash: str


@router.post("/v1/x402/settle")
def x402_settle(body: X402SettleRequest, db: Session = Depends(models.get_session)) -> dict:
    """Verify an on-chain USDC transfer and mark the ledger entry paid."""
    entry = (
        db.query(models.LedgerEntry)
        .filter_by(processor="x402", external_id=body.payment_id)
        .one_or_none()
    )
    if not entry:
        raise HTTPException(404, f"Unknown payment_id: {body.payment_id}")
    if entry.status == "succeeded":
        return {"ok": True, "already_settled": True, "ledger_entry_id": entry.id,
                "invoice_number": entry.invoice_number}

    amount_usdc = x402.usdc_amount_for(entry.amount_cents)
    result = x402.verify_usdc_transfer(
        tx_hash=body.tx_hash,
        expected_recipient=x402.RECEIVING_WALLET,
        expected_amount_usdc=amount_usdc,
    )
    if not result["valid"]:
        entry.status = "failed"
        entry.metadata_json = json.dumps({**(json.loads(entry.metadata_json or "{}")),
                                           "verification_error": result["reason"]})
        db.commit()
        raise HTTPException(402, f"x402 verification failed: {result['reason']}")

    _mark_paid(db, entry)
    db.commit()
    return {"ok": True, "ledger_entry_id": entry.id, "invoice_number": entry.invoice_number}


# ── Ledger read endpoints (AR use) ─────────────────────────────────────────

class LedgerEntryOut(BaseModel):
    id: str
    processor: str
    external_id: str
    status: str
    amount_cents: int
    currency: str
    customer_id: Optional[str]
    customer_email: Optional[str]
    invoice_number: Optional[str]
    description: Optional[str]
    due_at: Optional[datetime]
    paid_at: Optional[datetime]
    refunded_at: Optional[datetime]
    created_at: datetime


@router.get("/v1/ledger/{entry_id}", response_model=LedgerEntryOut)
def get_ledger_entry(entry_id: str, db: Session = Depends(models.get_session)) -> LedgerEntryOut:
    row = db.get(models.LedgerEntry, entry_id)
    if not row:
        raise HTTPException(404, "Ledger entry not found")
    return LedgerEntryOut(
        id=row.id, processor=row.processor, external_id=row.external_id,
        status=row.status, amount_cents=row.amount_cents, currency=row.currency,
        customer_id=row.customer_id, customer_email=row.customer_email,
        invoice_number=row.invoice_number, description=row.description,
        due_at=row.due_at, paid_at=row.paid_at, refunded_at=row.refunded_at,
        created_at=row.created_at,
    )


@router.get("/v1/ledger")
def list_ledger(
    status: Optional[str] = None,
    processor: Optional[str] = None,
    customer_email: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(models.get_session),
) -> dict:
    """AR list view. Filter by status / processor / email. Newest first."""
    q = db.query(models.LedgerEntry)
    if status:        q = q.filter(models.LedgerEntry.status == status)
    if processor:     q = q.filter(models.LedgerEntry.processor == processor)
    if customer_email: q = q.filter(models.LedgerEntry.customer_email == customer_email)
    rows = q.order_by(models.LedgerEntry.created_at.desc()).limit(min(limit, 500)).all()
    return {
        "count": len(rows),
        "data": [
            {
                "id": r.id, "processor": r.processor, "external_id": r.external_id,
                "status": r.status, "amount_cents": r.amount_cents, "currency": r.currency,
                "customer_email": r.customer_email, "invoice_number": r.invoice_number,
                "description": r.description, "due_at": r.due_at, "paid_at": r.paid_at,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


# ── AR views ──────────────────────────────────────────────────────────────

@router.get("/v1/ar/open")
def open_receivables(db: Session = Depends(models.get_session)) -> dict:
    """Pending + due — the AR worklist."""
    rows = db.query(models.LedgerEntry).filter(
        models.LedgerEntry.status == "pending",
        models.LedgerEntry.due_at.isnot(None),
    ).order_by(models.LedgerEntry.due_at.asc()).limit(500).all()
    return {
        "count": len(rows),
        "data": [
            {
                "id": r.id, "processor": r.processor, "amount_cents": r.amount_cents,
                "currency": r.currency, "customer_email": r.customer_email,
                "invoice_number": r.invoice_number, "description": r.description,
                "due_at": r.due_at,
            }
            for r in rows
        ],
    }


@router.get("/v1/ar/summary")
def ar_summary(db: Session = Depends(models.get_session)) -> dict:
    """AR rollup: total receivable, by processor, aging buckets."""
    from sqlalchemy import func
    rows = db.query(
        models.LedgerEntry.status,
        models.LedgerEntry.processor,
        func.count(models.LedgerEntry.id).label("n"),
        func.coalesce(func.sum(models.LedgerEntry.amount_cents), 0).label("total"),
    ).group_by(models.LedgerEntry.status, models.LedgerEntry.processor).all()
    return {
        "by_status_and_processor": [
            {
                "status": r.status, "processor": r.processor,
                "count": r.n, "total_cents": int(r.total),
            }
            for r in rows
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
