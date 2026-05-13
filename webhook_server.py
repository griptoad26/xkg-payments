"""
Webhook server for testing XKG payment integrations.
Receives webhooks from Stripe, LemonSqueezy, and processes x402 payments.
"""
from flask import Flask, request, jsonify
import hmac
import hashlib
import os
import json
from datetime import datetime

app = Flask(__name__)

# Configuration
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', 'whsec_test_secret')
LS_WEBHOOK_SECRET = os.getenv('LS_WEBHOOK_SECRET', 'ls_webhook_secret')
LOG_FILE = '/home/x2/github/xkg-payments/webhook_events.jsonl'

def log_event(event_type, data):
    """Log webhook events to file."""
    entry = {
        'timestamp': datetime.utcnow().isoformat(),
        'type': event_type,
        'data': data
    }
    with open(LOG_FILE, 'a') as f:
        f.write(json.dumps(entry) + '\n')

# ============ Stripe Webhook ============
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events."""
    payload = request.data
    sig = request.headers.get('Stripe-Signature', '')

    # Verify signature
    try:
        elements = dict(x.split('=', 1) for x in sig.split(','))
        timestamp = elements.get('t', '')
        expected_sig = elements.get('v1', '')

        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        computed_sig = hmac.new(
            STRIPE_WEBHOOK_SECRET.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(computed_sig, expected_sig):
            return jsonify({'error': 'Invalid signature'}), 400

    except Exception as e:
        # For testing, allow unverified if no secret set
        if STRIPE_WEBHOOK_SECRET == 'whsec_test_secret':
            pass
        else:
            return jsonify({'error': str(e)}), 400

    event = request.get_json()
    event_type = event.get('type', 'unknown')

    log_event(f'stripe_{event_type}', event)

    # Handle specific events
    if event_type == 'checkout.session.completed':
        session = event.get('data', {}).get('object', {})
        customer_email = session.get('customer_email', 'unknown')
        tier = session.get('metadata', {}).get('tier', 'unknown')
        print(f"[Stripe] Payment complete: {customer_email} bought {tier}")
        # TODO: Activate account

    elif event_type == 'customer.subscription.updated':
        sub = event.get('data', {}).get('object', {})
        print(f"[Stripe] Subscription updated: {sub.get('id')}")

    elif event_type == 'customer.subscription.deleted':
        sub = event.get('data', {}).get('object', {})
        print(f"[Stripe] Subscription cancelled: {sub.get('id')}")

    elif event_type == 'invoice.payment_failed':
        invoice = event.get('data', {}).get('object', {})
        print(f"[Stripe] Payment failed: {invoice.get('customer')}")

    return jsonify({'status': 'received', 'type': event_type})

# ============ LemonSqueezy Webhook ============
@app.route('/webhook/lemonsqueezy', methods=['POST'])
def lemonsqueezy_webhook():
    """Handle LemonSqueezy webhook events."""
    payload = request.data
    sig = request.headers.get('X-Signature', '')

    # Verify signature (LS uses HMAC-SHA256)
    if LS_WEBHOOK_SECRET != 'ls_webhook_secret':
        computed = hmac.new(
            LS_WEBHOOK_SECRET.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(computed, sig):
            return jsonify({'error': 'Invalid signature'}), 400

    event = request.get_json()
    event_name = event.get('meta', {}).get('event_name', 'unknown')

    log_event(f'ls_{event_name}', event)

    if event_name == 'order_created':
        order = event.get('data', {}).get('attributes', {})
        print(f"[LS] Order: {order.get('identifier')} - ${order.get('total')}")
        # TODO: Activate account

    elif event_name == 'subscription_created':
        sub = event.get('data', {}).get('attributes', {})
        print(f"[LS] Subscription: {sub.get('id')}")

    return jsonify({'status': 'received'})

# ============ Health Check ============
@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'webhook_server': 'active',
        'uptime': 'running'
    })

# ============ Dashboard ============
@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Simple webhook event dashboard."""
    events = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except:
                    pass

    # Return last 50 events
    recent = events[-50:] if len(events) > 50 else events

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>XKG Webhook Dashboard</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-gray-950 text-white p-8">
        <div class="max-w-6xl mx-auto">
            <h1 class="text-2xl font-bold mb-6">XKG Webhook Dashboard</h1>

            <div class="grid grid-cols-3 gap-4 mb-8">
                <div class="bg-gray-900 p-4 rounded-xl">
                    <p class="text-gray-400 text-sm">Total Events</p>
                    <p class="text-3xl font-bold">{len(events)}</p>
                </div>
                <div class="bg-gray-900 p-4 rounded-xl">
                    <p class="text-gray-400 text-sm">Recent 24h</p>
                    <p class="text-3xl font-bold">{len([e for e in recent if '2026-05' in e.get('timestamp', '')])血色}</p>
                </div>
                <div class="bg-gray-900 p-4 rounded-xl">
                    <p class="text-gray-400 text-sm">Sources</p>
                    <p class="text-3xl font-bold">2</p>
                </div>
            </div>

            <h2 class="text-lg font-semibold mb-4">Recent Events</h2>
            <div class="space-y-2">
    """

    for event in reversed(recent[-20:]):
        event_type = event.get('type', 'unknown')
        timestamp = event.get('timestamp', '')
        data = event.get('data', {})

        html += f"""
                <div class="bg-gray-900 p-3 rounded-lg flex items-center gap-4">
                    <span class="text-xs text-gray-500 w-40">{timestamp}</span>
                    <span class="px-2 py-1 rounded text-xs {'bg-green-600/20 text-green-400' if 'complete' in event_type else 'bg-gray-700 text-gray-300'}">
                        {event_type}
                    </span>
                </div>
        """

    html += """
            </div>
        </div>
    </body>
    </html>
    """

    return html

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
