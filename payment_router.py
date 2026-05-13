"""
Multi-payment processor router for XKG.
Routes payments to appropriate processor based on customer choice.
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any
from enum import Enum


class PaymentMethod(Enum):
    STRIPE = "stripe"
    LEMON_SQUEEZY = "lemon_squeezy"
    X402_CRYPTO = "x402"
    PAYPAL = "paypal"


@dataclass
class PaymentRequest:
    product_id: str
    amount_cents: int
    currency: str = "USD"
    customer_email: Optional[str] = None
    payment_method: PaymentMethod = PaymentMethod.STRIPE
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class PaymentResult:
    success: bool
    processor: str
    transaction_id: Optional[str] = None
    checkout_url: Optional[str] = None
    error: Optional[str] = None


class PaymentRouter:
    def __init__(self, config: Dict[str, Any]):
        self.config = config

        # Lazy-load processors
        self._stripe = None
        self._lemonsqueezy = None

    def process(self, request: PaymentRequest) -> PaymentResult:
        """Route payment to appropriate processor."""

        if request.payment_method == PaymentMethod.STRIPE:
            return self._stripe_checkout(request)
        elif request.payment_method == PaymentMethod.LEMON_SQUEEZY:
            return self._lemonsqueezy_checkout(request)
        elif request.payment_method == PaymentMethod.X402_CRYPTO:
            return self._x402_checkout(request)
        else:
            return PaymentResult(
                success=False,
                processor="unknown",
                error=f"Unsupported payment method: {request.payment_method}"
            )

    def _stripe_checkout(self, request: PaymentRequest) -> PaymentResult:
        """Standard Stripe checkout for fiat."""
        try:
            from xkg_stripe.checkout import create_checkout_session

            # Map product to Stripe price
            price_map = {
                'thick_client': 'price_thick_client_REPLACE',
                'vps_monthly': 'price_vps_monthly_REPLACE',
                'hardware_bundle': 'price_hardware_REPLACE',
            }
            price_id = price_map.get(request.product_id, price_map['thick_client'])

            url = create_checkout_session(
                price_id=price_id,  # Use actual Stripe price ID
                success_url=f"{self.config.get('success_url', 'https://xkg.ai/success')}?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{self.config.get('cancel_url', 'https://xkg.ai/pricing')}",
                customer_email=request.customer_email,
            )

            return PaymentResult(
                success=True,
                processor="stripe",
                checkout_url=url.get('url'),
            )
        except Exception as e:
            return PaymentResult(success=False, processor="stripe", error=str(e))

    def _lemonsqueezy_checkout(self, request: PaymentRequest) -> PaymentResult:
        """LemonSqueezy checkout with crypto support."""
        try:
            import requests

            # LemonSqueezy API
            lemonsqueezy_config = self.config.get('lemon_squeezy', {})
            api_key = lemonsqueezy_config.get('api_key', '')
            store_id = lemonsqueezy_config.get('store_id', '')

            # Product variants in LemonSqueezy
            variant_map = {
                'thick_client': 'REPLACE_WITH_VARIANT_ID',
                'vps_monthly': 'REPLACE_WITH_VARIANT_ID',
                'hardware_bundle': 'REPLACE_WITH_VARIANT_ID',
            }
            variant_id = variant_map.get(request.product_id, variant_map['thick_client'])

            # Create checkout
            url = f"https://app.lemonsqueezy.com/checkout/buy/{variant_id}"

            # For crypto, LemonSqueezy has built-in crypto checkout
            # The URL itself supports crypto payment method selection
            checkout_url = f"{url}?checkout[email]={request.customer_email or ''}"

            return PaymentResult(
                success=True,
                processor="lemon_squeezy",
                checkout_url=checkout_url,
            )
        except Exception as e:
            return PaymentResult(success=False, processor="lemon_squeezy", error=str(e))

    def _x402_checkout(self, request: PaymentRequest) -> PaymentResult:
        """x402 direct crypto payment - no processor."""
        # x402 is a protocol, not a processor
        # Customer sends crypto directly to your address with x402 header

        x402_config = self.config.get('x402', {})
        receiving_address = x402_config.get('receiving_address', '')

        # For x402, we generate a payment request that includes:
        # 1. Receiving address
        # 2. Amount to send
        # 3. Payment URL/instructions

        # Map products to crypto amounts (in cents equivalent)
        crypto_amounts = {
            'thick_client': {'amount': 4900, 'unit': 'cents_USD'},  # $49
            'vps_monthly': {'amount': 900, 'unit': 'cents_USD'},     # $9
            'hardware_bundle': {'amount': 29900, 'unit': 'cents_USD'},  # $299
        }

        amount_info = crypto_amounts.get(request.product_id, crypto_amounts['thick_client'])

        return PaymentResult(
            success=True,
            processor="x402",
            checkout_url=None,  # x402 doesn't use checkout URLs
            metadata={
                'receiving_address': receiving_address,
                'amount_requested': amount_info['amount'],
                'unit': amount_info['unit'],
                'payment_instruction': f"Send {amount_info['amount']/100} USD equivalent to {receiving_address}",
                'protocol': 'x402',
            }
        )