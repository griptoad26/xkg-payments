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
    # ── Stripe ────────────────────────────────────────────────────────────
    stripe_secret_key:        str = field(default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", "sk_test_placeholder"))
    stripe_publishable_key:   str = field(default_factory=lambda: os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_test_placeholder"))
    stripe_webhook_secret:    str = field(default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_placeholder"))
    stripe_api_version:       str = field(default_factory=lambda: os.environ.get("STRIPE_API_VERSION", "2025-03-31.basil"))
    stripe_test_mode:         bool = field(default_factory=lambda: _bool(os.environ.get("STRIPE_TEST_MODE"), True))

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
