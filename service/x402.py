"""x402 payment protocol — USDC on Base.

Ported and adapted from xkg-stripe/x402_routes.py. Clean module-level
functions instead of Flask route handlers, so we can reuse them from
the unified checkout dispatcher in service/checkout.py.

Wire format:
  - GET /.well-known/x402              discovery
  - GET /v1/x402/challenge/{plan}      legacy direct challenge (kept for back-compat)
  - POST /v1/x402/settle               verify + mark paid

The unified `/v1/checkout` endpoint (POST with method=x402) builds the
challenge inline via build_challenge() here.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import requests

# Receiving wallet — default is the XKG testnet treasury on Base Sepolia.
# Override with X402_WALLET env var for mainnet or a different treasury.
RECEIVING_WALLET = os.environ.get(
    "X402_WALLET",
    "0x3D2f7EDeB6e579447Fd5d00D05578041469D79e0",  # XKG testnet treasury (Base Sepolia)
)
# The default placeholder zero-address is intentionally rejected on import
# so the service refuses to start with a misconfigured wallet.
_PLACEHOLDER = "0x0000000000000000000000000000000000000000"
if RECEIVING_WALLET == _PLACEHOLDER:
    raise RuntimeError(
        "X402_WALLET must be set to a real address. Refusing to start with the zero address."
    )

# USDC contract addresses
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# Public Base RPC endpoints (no auth, low-volume OK)
BASE_RPC = "https://mainnet.base.org"
BASE_SEPOLIA_RPC = "https://sepolia.base.org"

# Network selection: "base" (mainnet) or "base-sepolia" (testnet, default for safety)
NETWORK = os.environ.get("X402_NETWORK", "base-sepolia")
RPC_URL = BASE_SEPOLIA_RPC if NETWORK == "base-sepolia" else BASE_RPC
USDC_ADDR = USDC_BASE_SEPOLIA if NETWORK == "base-sepolia" else USDC_BASE

# USDC has 6 decimals
USDC_DECIMALS = 6


def usdc_amount_for(usd_cents: int) -> int:
    """Convert USD cents → USDC base units (6 decimals).
    $0.01 = 100_000 micro-USDC, assuming $1 USDC ≈ $1 USD.
    """
    return usd_cents * 10_000


def build_challenge(plan: str, cfg: dict) -> dict:
    """Build the 402 Payment Required challenge response.

    cfg: {"amount": int (USD cents), "name": str}
    Returns the modern Coinbase-style challenge JSON, with legacy
    RFC-9420 headers attached under "headers" for back-compat.
    """
    payment_id = generate_payment_id(plan)
    amount_usdc = usdc_amount_for(cfg["amount"])
    expires = datetime.now(timezone.utc) + timedelta(hours=1)

    return {
        "x402Version": 1,
        "payment_id": payment_id,
        "scheme": "exact",
        "network": NETWORK,
        "resource": f"/v1/x402/settle?payment_id={payment_id}",
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK,
                "maxAmountRequired": str(amount_usdc),
                "resource": f"/v1/x402/settle?payment_id={payment_id}",
                "description": cfg["name"],
                "mimeType": "application/json",
                "payTo": RECEIVING_WALLET,
                "asset": USDC_ADDR,
                "maxTimeoutSeconds": 3600,
                "extra": {
                    "name": "USD Coin",
                    "symbol": "USDC",
                    "decimals": USDC_DECIMALS,
                },
            }
        ],
        # Legacy RFC-9420 headers (for older x402 clients):
        "headers": {
            "Payment-Required": json.dumps({
                "amount": cfg["amount"] / 100,
                "currency": "USD",
                "product": plan,
                "payment_id": payment_id,
            }),
            "Payment-Methods": json.dumps([{
                "scheme": "ethereum",
                "amount": f"{cfg['amount']/100:.2f} USD",
                "address": RECEIVING_WALLET,
                "asset": "USDC",
                "network": NETWORK,
                "expires": expires.isoformat(),
            }]),
            "Payment-Retry-After": "3600",
        },
    }


def generate_payment_id(plan: str, email: str = "") -> str:
    """Unique payment id for a challenge. Includes plan so humans can read logs."""
    rand = secrets.token_urlsafe(8)
    return f"x402-{plan}-{rand}"


def verify_usdc_transfer(tx_hash: str, expected_recipient: str,
                          expected_amount_usdc: int,
                          min_confirmations: int = 1) -> dict:
    """Verify a USDC transfer on Base via the public RPC.

    Returns: {"valid": bool, "reason": str, "details": dict}
    """
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return {"valid": False, "reason": "tx hash must be 0x + 64 hex chars"}

    try:
        resp = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
            "params": [tx_hash], "id": 1,
        }, timeout=10)
        tx = resp.json().get("result")
    except Exception as e:
        return {"valid": False, "reason": f"RPC error: {e}"}

    if not tx:
        return {"valid": False, "reason": "transaction not found"}

    # Must be a USDC transfer (input data starts with transfer(address,address,uint256) = 0xa9059cbb)
    input_data = tx.get("input", "")
    if not input_data.startswith("0xa9059cbb"):
        return {"valid": False, "reason": "not a USDC transfer (no transfer() call)"}
    if len(input_data) < 138:
        return {"valid": False, "reason": "input data too short for transfer()"}

    # Parse transfer(address,address,uint256) input
    recipient = "0x" + input_data[36:76][-40:]
    amount_hex = input_data[76:138]
    amount_usdc_base = int(amount_hex, 16)

    if tx.get("to", "").lower() != USDC_ADDR.lower():
        return {"valid": False, "reason": f"tx target is not USDC contract ({tx.get('to')})"}
    if recipient.lower() != expected_recipient.lower():
        return {"valid": False, "reason": f"recipient {recipient} != expected {expected_recipient}"}
    if amount_usdc_base < expected_amount_usdc:
        return {"valid": False,
                "reason": f"amount {amount_usdc_base/(10**USDC_DECIMALS)} < required ${expected_amount_usdc/(10**USDC_DECIMALS)}"}

    try:
        block_resp = requests.post(RPC_URL, json={
            "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1,
        }, timeout=10)
        current_block = int(block_resp.json().get("result", "0x0"), 16)
        tx_block = int(tx.get("blockNumber", "0x0"), 16)
        confirmations = current_block - tx_block + 1
    except Exception:
        confirmations = 0

    return {
        "valid": True,
        "reason": "ok",
        "details": {
            "tx_hash": tx_hash,
            "block_number": tx.get("blockNumber"),
            "from": tx.get("from"),
            "to": tx.get("to"),
            "recipient": recipient,
            "amount_usdc": amount_usdc_base / (10 ** USDC_DECIMALS),
            "confirmations": confirmations,
            "network": NETWORK,
        },
    }
