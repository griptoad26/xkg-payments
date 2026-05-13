# XKG Multi-Payment Processor

Accepts: Stripe (fiat), LemonSqueezy (fiat + crypto), x402 (direct crypto)

## Quick Start

1. Copy `payment_config.example.yaml` to `payment_config.yaml`
2. Fill in your API keys and receiving addresses
3. Run: `pip install -r requirements.txt`

## Payment Methods

### Stripe (Fiat)
- Best for: Credit/debit cards, Apple Pay, Google Pay
- Fee: 2.9% + 30¢
- Setup: Create products in Stripe dashboard, get price IDs

### LemonSqueezy (Fiat + Crypto)
- Best for: International, crypto users
- Fee: 3% + 30¢ + 0.5% crypto fee
- Setup: Create store at app.lemonsqueezy.com

### x402 Protocol (Direct Crypto)
- Best for: Privacy-conscious, crypto-native users
- Fee: 0% (you keep everything)
- Setup: Set up receiving addresses in config

## Usage

```python
from payment_router import PaymentRouter, PaymentRequest, PaymentMethod

router = PaymentRouter(config)

# Stripe checkout
result = router.process(PaymentRequest(
    product_id='thick_client',
    amount_cents=4900,
    customer_email='customer@example.com',
    payment_method=PaymentMethod.STRIPE
))

# x402 crypto payment
result = router.process(PaymentRequest(
    product_id='thick_client',
    amount_cents=4900,
    customer_email='customer@example.com',
    payment_method=PaymentMethod.X402_CRYPTO
))
```

## x402 Protocol

x402 (RFC 9420) is an HTTP payment protocol. The server responds with 402
and includes Payment-Methods header listing accepted payment addresses.

The client then makes the payment (on-chain) and includes the payment
proof (transaction hash) in the Authorization header.

Example flow:
1. Client requests /download/thick_client
2. Server responds: 402 Payment Required, Payment-Methods: [btc: bc1q..., eth: 0x...]
3. Client sends payment to bitcoin address
4. Client retries with Authorization: 402 bitcoin tx_hash
5. Server validates tx, serves content

See x402_handler.py for full implementation.