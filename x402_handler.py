"""
x402 protocol handler for direct crypto payments.

x402 (RFC 9420) is an HTTP payment protocol that allows payment through
the HTTP Authorization header using the scheme '402 Payment Required'.

The server responds with:
- 402 Payment Required (if no valid payment header)
- Payment-Methods header listing accepted methods
- Payment-Retry-After header with retry info

Reference: https://datatracker.ietf.org/doc/html/rfc9420
"""
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json


@dataclass
class PaymentMethod:
    scheme: str  # 'bitcoin', 'ethereum', 'paypal', etc.
    amount: str  # Amount to pay (e.g., "49.00 USD")
    address: str  # Payment address (wallet address, email, etc.)
    expires: Optional[str] = None  # ISO timestamp
    metadata: Optional[Dict] = None


@dataclass
class PaymentInfo:
    required: bool
    methods: List[PaymentMethod]
    retry_after: Optional[int] = None  # seconds


class X402Handler:
    """
    Handles x402 (RFC 9420) payment protocol.

    Use this to:
    1. Generate payment challenge for unauthenticated requests
    2. Validate x402 payment headers
    3. Process successful payments
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.receiving_addresses = config.get('x402_addresses', {})

    def generate_payment_challenge(
        self,
        amount_cents: int,
        currency: str = "USD",
        product_id: str = ""
    ) -> Dict[str, Any]:
        """
        Generate a 402 Payment Required challenge response.

        Returns dict for building the HTTP response with proper headers.
        """
        # Map product to receiving address
        address = self.receiving_addresses.get(product_id,
            self.receiving_addresses.get('default', ''))

        # Generate payment methods for different crypto
        methods = []

        # Bitcoin
        if 'bitcoin' in self.receiving_addresses:
            methods.append(PaymentMethod(
                scheme='bitcoin',
                amount=f"{amount_cents/100:.2f} USD",  # Display in USD equivalent
                address=self.receiving_addresses['bitcoin'],
                expires=self._iso_future(hours=24),
                metadata={'product_id': product_id, 'fiat_amount_cents': amount_cents}
            ))

        # Ethereum/LERC20
        if 'ethereum' in self.receiving_addresses:
            methods.append(PaymentMethod(
                scheme='ethereum',
                amount=f"{amount_cents/100:.2f} USD",
                address=self.receiving_addresses['ethereum'],
                expires=self._iso_future(hours=24),
                metadata={'product_id': product_id, 'fiat_amount_cents': amount_cents}
            ))

        # PayPal (x402 can handle PayPal too)
        if 'paypal' in self.receiving_addresses:
            methods.append(PaymentMethod(
                scheme='paypal',
                amount=f"{amount_cents/100:.2f} USD",
                address=self.receiving_addresses['paypal'],
                expires=self._iso_future(hours=24),
                metadata={'product_id': product_id}
            ))

        # Build challenge headers
        payment_info = PaymentInfo(
            required=True,
            methods=methods,
            retry_after=3600  # 1 hour
        )

        return {
            'status_code': 402,
            'headers': {
                'Payment-Required': json.dumps({
                    'amount': f"{amount_cents/100:.2f}",
                    'currency': currency,
                    'product': product_id
                }),
                'Payment-Methods': json.dumps([{
                    'scheme': m.scheme,
                    'amount': m.amount,
                    'address': m.address,
                    'expires': m.expires,
                    'metadata': m.metadata
                } for m in methods]),
                'Payment-Retry-After': str(payment_info.retry_after),
            },
            'body': f"Payment required to access this resource. Amount: ${amount_cents/100:.2f} USD"
        }

    def validate_payment_header(self, auth_header: str) -> bool:
        """
        Validate an x402 payment header.

        The header format is:
        Authorization: 402 <scheme> <proof>

        Where proof is a payment receipt/proof in the scheme's format.
        For crypto, this would be a transaction hash + signature.
        """
        if not auth_header:
            return False

        if not auth_header.startswith('402 '):
            return False

        # Parse the header
        parts = auth_header[4:].split(' ', 1)
        if len(parts) < 2:
            return False

        scheme = parts[0]
        proof = parts[1]

        # For crypto, validate the proof (transaction hash)
        # Different validation for different schemes
        validators = {
            'bitcoin': self._validate_bitcoin,
            'ethereum': self._validate_ethereum,
            'paypal': self._validate_paypal,
        }

        validator = validators.get(scheme, lambda x: False)
        return validator(proof)

    def _validate_bitcoin(self, proof: str) -> bool:
        """
        Validate Bitcoin payment proof (transaction hash).

        In production, would query blockchain API to verify:
        1. Transaction exists
        2. Sent to correct address
        3. Amount meets requirements
        4. Confirmations >= required (e.g., 1 for small, 6 for large)
        """
        # Placeholder: In production, call blockchain API
        # e.g., blockcypher, blockstream, etc.
        tx_hash = proof

        # Basic validation: 64 char hex string (Bitcoin tx hash)
        if len(tx_hash) != 64:
            return False
        try:
            int(tx_hash, 16)
            return True
        except ValueError:
            return False

    def _validate_ethereum(self, proof: str) -> bool:
        """
        Validate Ethereum payment proof (transaction hash).

        Would call Ethereum RPC/API to verify transaction.
        """
        tx_hash = proof

        # ETH tx hashes are 66 chars (0x + 64 hex)
        if len(tx_hash) != 66 or not tx_hash.startswith('0x'):
            return False
        try:
            int(tx_hash, 16)
            return True
        except ValueError:
            return False

    def _validate_paypal(self, proof: str) -> bool:
        """
        Validate PayPal payment proof (transaction ID or email).
        """
        # PayPal proof could be transaction ID or some other format
        return len(proof) > 5

    def _iso_future(self, hours: int = 24) -> str:
        """Return ISO timestamp N hours in future."""
        return (datetime.utcnow() + timedelta(hours=hours)).isoformat() + 'Z'


# Example x402 middleware for Flask/FastAPI
def x402_middleware(app, handler, x402_config):
    """
    Flask middleware that enforces x402 payment on specific routes.

    Usage:
        @app.route('/download/<product_id>')
        @x402_middleware(app, require_payment_for=['thick_client', 'vps_monthly'])
        def download(product_id):
            return send_file(...)
    """
    from functools import wraps

    def decorated(handler):
        @wraps(handler)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get('Authorization', '')

            x402_handler = X402Handler({'x402_addresses': x402_config})

            if not auth_header.startswith('402 '):
                # No payment header - generate challenge
                product_id = kwargs.get('product_id', 'default')
                amount = x402_config.get('prices', {}).get(product_id, 4900)  # cents

                challenge = x402_handler.generate_payment_challenge(
                    amount_cents=amount,
                    product_id=product_id
                )

                return jsonify(challenge['body']), challenge['status_code'], challenge['headers']

            # Validate payment
            if x402_handler.validate_payment_header(auth_header):
                return handler(*args, **kwargs)
            else:
                return jsonify({'error': 'Invalid payment proof'}), 402

        return wrapper
    return decorated