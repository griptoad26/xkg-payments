"""Thin wrapper around the Stripe Python SDK.

Centralises API key handling, error normalisation, and pagination so the
HTTP routes stay clean.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

import stripe
# Stripe 15.x moved error classes to stripe._error (private but stable across
# the 12.x→15.x line). Fall back to stripe.error for older installs.
try:
    from stripe._error import (
        AuthenticationError as StripeAuthError,
        CardError, InvalidRequestError, RateLimitError, StripeError,
    )
except ImportError:  # pragma: no cover
    from stripe.error import (
        AuthenticationError as StripeAuthError,
        CardError, InvalidRequestError, RateLimitError, StripeError,
    )

from .config import settings

log = logging.getLogger("xkg-payments.stripe")

# Configure the SDK once at import time. Safe to call multiple times.
stripe.api_key = settings.stripe_secret_key
if settings.stripe_api_version:
    stripe.api_version = settings.stripe_api_version  # type: ignore[attr-defined]
stripe.max_network_retries = 2  # SDK-level retries for 5xx


class StripeError(Exception):
    """Normalised exception with safe message and code."""
    def __init__(self, code: str, message: str, status_code: int = 400, raw: Any = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.raw = raw
        super().__init__(message)


def _wrap(e: StripeError) -> StripeError:
    """Convert SDK exception to our normalised type."""
    if isinstance(e, CardError):
        return StripeError("card_error", e.user_message or str(e), 402, e)
    if isinstance(e, StripeAuthError):
        return StripeError("auth_error", "Invalid Stripe API key", 401, e)
    if isinstance(e, RateLimitError):
        return StripeError("rate_limited", "Stripe rate limit hit — try again shortly", 429, e)
    if isinstance(e, InvalidRequestError):
        return StripeError("invalid_request", str(e), 400, e)
    return StripeError("stripe_error", str(e), 500, e)


# ── Customers ──────────────────────────────────────────────────────────────

def create_customer(email: str, name: Optional[str] = None,
                    metadata: Optional[dict] = None) -> dict:
    try:
        return dict(stripe.Customer.create(
            email=email, name=name, metadata=metadata or {}
        ))
    except StripeError as e:
        raise _wrap(e)


def get_customer(stripe_customer_id: str) -> dict:
    try:
        return dict(stripe.Customer.retrieve(stripe_customer_id))
    except StripeError as e:
        raise _wrap(e)


# ── Subscriptions ──────────────────────────────────────────────────────────

def create_subscription(customer_id: str, price_id: str,
                       trial_days: Optional[int] = None,
                       metadata: Optional[dict] = None) -> dict:
    params: dict = {"customer": customer_id, "items": [{"price": price_id}]}
    if trial_days:
        params["trial_period_days"] = trial_days
    if metadata:
        params["metadata"] = metadata
    try:
        return dict(stripe.Subscription.create(**params))
    except StripeError as e:
        raise _wrap(e)


def cancel_subscription(stripe_subscription_id: str,
                        at_period_end: bool = True) -> dict:
    try:
        if at_period_end:
            return dict(stripe.Subscription.modify(
                stripe_subscription_id, cancel_at_period_end=True
            ))
        return dict(stripe.Subscription.cancel(stripe_subscription_id))
    except StripeError as e:
        raise _wrap(e)


def get_subscription(stripe_subscription_id: str) -> dict:
    try:
        return dict(stripe.Subscription.retrieve(stripe_subscription_id))
    except StripeError as e:
        raise _wrap(e)


# ── Products & Prices (cached locally) ────────────────────────────────────

def list_products(active_only: bool = True) -> Iterator[dict]:
    params: dict = {"limit": 100}
    if active_only:
        params["active"] = True
    try:
        for p in stripe.Product.list(**params).auto_paging_iter():
            yield dict(p)
    except StripeError as e:
        raise _wrap(e)


def list_prices(product_stripe_id: str, active_only: bool = True) -> Iterator[dict]:
    params: dict = {"product": product_stripe_id, "limit": 100}
    if active_only:
        params["active"] = True
    try:
        for p in stripe.Price.list(**params).auto_paging_iter():
            yield dict(p)
    except StripeError as e:
        raise _wrap(e)


# ── Webhook signature verification ────────────────────────────────────────

def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify the Stripe-Signature header and return the parsed event dict.

    Raises StripeError(400) if the signature is invalid.
    """
    if not settings.stripe_webhook_secret or settings.stripe_webhook_secret == "whsec_placeholder":
        raise StripeError("webhook_misconfigured", "STRIPE_WEBHOOK_SECRET is not set", 500)
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
        return dict(event)
    except ValueError as e:
        raise StripeError("invalid_payload", f"Could not parse JSON: {e}", 400, e)
    except stripe.SignatureVerificationError as e:  # type: ignore[attr-defined]
        raise StripeError("bad_signature", "Webhook signature verification failed", 400, e)


# ── PaymentIntents (one-off card charges) ──────────────────────────────────

def create_payment_intent(amount_cents: int, currency: str = "usd",
                           description: Optional[str] = None,
                           receipt_email: Optional[str] = None,
                           metadata: Optional[dict] = None,
                           automatic_payment_methods: bool = True,
                           idempotency_key: Optional[str] = None) -> dict:
    """Create a one-off PaymentIntent for card / wallet payments.

    The returned dict has at least: id, status, client_secret.
    Pass `idempotency_key` to safely retry; Stripe will return the same
    intent for the same key within 24h.
    """
    params: dict = {
        "amount": amount_cents,
        "currency": currency.lower(),
    }
    if description:
        params["description"] = description
    if receipt_email:
        params["receipt_email"] = receipt_email
    if metadata:
        params["metadata"] = metadata
    if automatic_payment_methods:
        params["automatic_payment_methods"] = {"enabled": True}
    create_kwargs: dict = {}
    if idempotency_key:
        create_kwargs["idempotency_key"] = idempotency_key
    try:
        intent = stripe.PaymentIntent.create(**params, **create_kwargs)
        return {
            "id": intent["id"],
            "status": intent["status"],
            "client_secret": intent.get("client_secret"),
            "amount": intent["amount"],
            "currency": intent["currency"],
        }
    except StripeError as e:
        raise _wrap(e)
