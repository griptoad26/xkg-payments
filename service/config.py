"""xkg-payments service configuration.

Loads from environment variables. All values have safe defaults for local
development, but production deployments MUST set the *KEY and
STRIPE_WEBHOOK_SECRET values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None: return default
    return v.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # ── Stripe (active keys — what the SDK actually uses) ─────────────────
    stripe_secret_key:        str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", "sk_test_placeholder"))
    stripe_publishable_key:   str = field(default_factory=lambda: os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_test_placeholder"))
    stripe_webhook_secret:    str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_placeholder"))
    stripe_api_version:       str = field(default_factory=lambda: os.environ.get("STRIPE_API_VERSION", "2025-03-31.basil"))
    stripe_test_mode:         bool = field(default_factory=lambda: _bool(os.environ.get("STRIPE_TEST_MODE"), True))

    # ── Stripe (live-mode readiness — used ONLY by /v1/admin/stripe-readiness) ──
    # These are NEVER used by the SDK; the readiness check just confirms
    # the operator has the live credentials staged and ready to flip.
    stripe_live_pk:           str = field(default_factory=lambda: os.environ.get("STRIPE_LIVE_PK", ""))
    stripe_live_sk:           str = field(default_factory=lambda: os.environ.get("STRIPE_LIVE_SK", ""))
    stripe_live_webhook_secret: str = field(default_factory=lambda: os.environ.get("STRIPE_LIVE_WEBHOOK_SECRET", ""))

    # ── Admin (gates /v1/admin/* endpoints — readiness checks, etc.) ──────
    # Set to a long random secret in prod. Empty == admin endpoints disabled.
    admin_token:              str = field(default_factory=lambda: os.environ.get("X_ADMIN_TOKEN", ""))

    # ── Service ───────────────────────────────────────────────────────────
    host:                     str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    port:                     int = field(default_factory=lambda: int(os.environ.get("PORT", "8765")))
    database_url:             str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./xkg-payments.db"))
    log_level:                str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    # ── Auth ──────────────────────────────────────────────────────────────
    # Bearer tokens that can call the REST API (the desktop client uses one)
    api_bearer_tokens:        List[str] = field(
        default_factory=lambda: [t for t in os.environ.get("API_BEARER_TOKENS", "devtoken-change-me").split(",") if t]
    )

    # ── Limits ────────────────────────────────────────────────────────────
    rate_limit_per_minute:    int = field(default_factory=lambda: int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")))

    def is_production(self) -> bool:
        return not self.stripe_test_mode and self.stripe_secret_key.startswith("sk_live_")

    def live_readiness(self) -> dict:
        """Dry-run check for whether the service is ready to flip to live Stripe.

        Does NOT flip anything. Only reports what env vars are present and what
        would need to change. Safe to call anytime.
        """
        has_live_sk = self.stripe_live_sk.startswith("sk_live_") and len(self.stripe_live_sk) > 12
        has_live_pk = self.stripe_live_pk.startswith("pk_live_") and len(self.stripe_live_pk) > 12
        has_live_webhook = self.stripe_live_webhook_secret.startswith("whsec_") and len(self.stripe_live_webhook_secret) > 8

        blockers: list[str] = []
        if self.stripe_test_mode:
            blockers.append("STRIPE_TEST_MODE is still true — set STRIPE_TEST_MODE=false to go live")
        if not has_live_sk:
            blockers.append("STRIPE_LIVE_SK missing or malformed (must start with sk_live_)")
        if not has_live_pk:
            blockers.append("STRIPE_LIVE_PK missing or malformed (must start with pk_live_)")
        if not has_live_webhook:
            blockers.append("STRIPE_LIVE_WEBHOOK_SECRET missing or malformed (must start with whsec_)")
        if self.stripe_secret_key == "sk_test_placeholder":
            blockers.append("STRIPE_SECRET_KEY is still the placeholder — replace with sk_live_* (or sk_test_* for staging)")
        if self.api_bearer_tokens == ["devtoken-change-me"]:
            blockers.append("API_BEARER_TOKENS still default — rotate before live")
        if not self.admin_token:
            blockers.append("X_ADMIN_TOKEN not set — admin endpoints are disabled (recommended for live)")

        return {
            "test_mode": self.stripe_test_mode,
            "has_live_pk": has_live_pk,
            "has_live_sk": has_live_sk,
            "has_live_webhook_secret": has_live_webhook,
            "has_webhook_secret": self.stripe_webhook_secret not in ("", "whsec_placeholder"),
            "active_sk_prefix": self.stripe_secret_key[:7] + "...",
            "active_pk_prefix": self.stripe_publishable_key[:7] + "...",
            "api_version": self.stripe_api_version,
            "ready_for_live": len(blockers) == 0,
            "blockers": blockers,
        }

    def safe_summary(self) -> dict:
        """Return a redacted view for logging."""
        return {
            "host": self.host,
            "port": self.port,
            "test_mode": self.stripe_test_mode,
            "sk_prefix": self.stripe_secret_key[:7] + "...",
            "pk_prefix": self.stripe_publishable_key[:7] + "...",
            "whsec_set": self.stripe_webhook_secret != "whsec_placeholder",
            "api_version": self.stripe_api_version,
            "n_bearer_tokens": len(self.api_bearer_tokens),
        }


settings = Settings()
