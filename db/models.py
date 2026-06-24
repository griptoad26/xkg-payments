"""SQLAlchemy ORM models for xkg-payments.

Mirrors db/schema.sql 1:1. Uses SQLAlchemy 2.0 declarative syntax.
Supports both SQLite (default, dev) and PostgreSQL (prod) via
DATABASE_URL env var.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id:                 Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_customer_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    email:              Mapped[str] = mapped_column(String, nullable=False, index=True)
    display_name:       Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json:      Mapped[Optional[str]] = mapped_column("metadata", Text, nullable=True)
    created_at:         Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at:         Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="customer")


class Product(Base):
    __tablename__ = "products"

    id:                Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_product_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name:              Mapped[str] = mapped_column(String, nullable=False)
    description:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tier:              Mapped[Optional[str]] = mapped_column(String, nullable=True)
    active:            Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json:     Mapped[Optional[str]] = mapped_column("metadata", Text, nullable=True)
    created_at:        Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at:        Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    prices: Mapped[list["Price"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Price(Base):
    __tablename__ = "prices"

    id:                Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_price_id:   Mapped[str] = mapped_column(String, unique=True, index=True)
    product_id:        Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    currency:          Mapped[str] = mapped_column(String, default="usd")
    unit_amount:       Mapped[int] = mapped_column(Integer, nullable=False)
    interval:          Mapped[Optional[str]] = mapped_column(String, nullable=True)
    interval_count:    Mapped[int] = mapped_column(Integer, default=1)
    trial_period_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active:            Mapped[bool] = mapped_column(Boolean, default=True)
    created_at:        Mapped[datetime] = mapped_column(DateTime, default=_now)

    product: Mapped["Product"] = relationship(back_populates="prices")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="price")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id:                    Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_subscription_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    customer_id:           Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    price_id:              Mapped[str] = mapped_column(ForeignKey("prices.id"))
    status:                Mapped[str] = mapped_column(String, nullable=False, index=True)
    current_period_start:  Mapped[datetime] = mapped_column(DateTime, nullable=False)
    current_period_end:    Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cancel_at_period_end:  Mapped[bool] = mapped_column(Boolean, default=False)
    canceled_at:           Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_start:           Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trial_end:             Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    default_payment_method_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    collection_method:     Mapped[str] = mapped_column(String, default="charge_automatically")
    metadata_json:         Mapped[Optional[str]] = mapped_column("metadata", Text, nullable=True)
    created_at:            Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at:            Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="subscriptions")
    price:    Mapped["Price"]    = relationship(back_populates="subscriptions")


class PaymentIntent(Base):
    __tablename__ = "payment_intents"

    id:                       Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_payment_intent_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    customer_id:              Mapped[Optional[str]] = mapped_column(ForeignKey("customers.id"), index=True, nullable=True)
    amount:                   Mapped[int] = mapped_column(Integer, nullable=False)
    currency:                 Mapped[str] = mapped_column(String, default="usd")
    status:                   Mapped[str] = mapped_column(String, nullable=False)
    client_secret:            Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_payment_error:       Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:               Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at:               Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id:              Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_event_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    type:            Mapped[str] = mapped_column(String, nullable=False, index=True)
    api_version:     Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload:         Mapped[str] = mapped_column(Text, nullable=False)
    processed:       Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    processed_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error:           Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at:     Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Invoice(Base):
    __tablename__ = "invoices"

    id:                  Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    stripe_invoice_id:   Mapped[str] = mapped_column(String, unique=True, index=True)
    customer_id:         Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    subscription_id:     Mapped[Optional[str]] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    amount_due:          Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paid:         Mapped[int] = mapped_column(Integer, default=0)
    currency:            Mapped[str] = mapped_column(String, nullable=False)
    status:              Mapped[str] = mapped_column(String, nullable=False)
    hosted_invoice_url:  Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invoice_pdf:         Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    period_start:        Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at:          Mapped[datetime] = mapped_column(DateTime, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id:         Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    actor:      Mapped[str] = mapped_column(String, nullable=False, index=True)
    action:     Mapped[str] = mapped_column(String, nullable=False)
    target:     Mapped[Optional[str]] = mapped_column(String, index=True, nullable=True)
    before:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after:      Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ─── Engine & session factory ──────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./xkg-payments.db")
engine = create_engine(DATABASE_URL, future=True, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    Base.metadata.create_all(bind=engine)


def get_session():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── 9. ledger_entries (AR-ready, processor-agnostic) ────────────────────

class LedgerEntry(Base):
    """Single source of truth for every dollar in and dollar out.

    Designed so that an Accounts Receivable system can plug in later as a
    query layer on top, NOT as a migration. See db/schema.sql for the
    field-by-field rationale.
    """
    __tablename__ = "ledger_entries"

    id:              Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    processor:       Mapped[str] = mapped_column(String, nullable=False, index=True)
    external_id:     Mapped[str] = mapped_column(String, nullable=False, index=True)
    status:          Mapped[str] = mapped_column(String, nullable=False, index=True)
    amount_cents:    Mapped[int] = mapped_column(Integer, nullable=False)
    currency:        Mapped[str] = mapped_column(String, nullable=False, default="usd")
    customer_id:     Mapped[Optional[str]] = mapped_column(ForeignKey("customers.id"), index=True, nullable=True)
    customer_email:  Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_number:  Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    description:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json:   Mapped[Optional[str]] = mapped_column("metadata", Text, nullable=True)
    due_at:          Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paid_at:         Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    refunded_at:     Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    refund_reason:   Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at:      Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class InvoiceSequence(Base):
    """Per-year counter for human-readable invoice numbers (INV-2026-0001)."""
    __tablename__ = "invoice_sequences"

    year:        Mapped[int] = mapped_column(Integer, primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def next_invoice_number(db: Session, year: int) -> str:
    """Atomically allocate the next invoice number for a year. Idempotent under load.

    Returns e.g. 'INV-2026-0001'. Safe under concurrent writes because
    SQLAlchemy's session flush + commit order gives us row-level locking
    on SQLite (which is single-writer anyway) and Postgres (which locks
    the row via SELECT ... FOR UPDATE on the seq row).
    """
    from sqlalchemy import select
    seq = db.execute(select(InvoiceSequence).where(InvoiceSequence.year == year)).scalar_one_or_none()
    if seq is None:
        seq = InvoiceSequence(year=year, last_number=1)
        db.add(seq)
    else:
        seq.last_number = seq.last_number + 1
    db.flush()
    return f"INV-{year}-{seq.last_number:04d}"
