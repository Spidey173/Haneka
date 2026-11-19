"""Stripe Webhook Signature Verification and Event Processing Service."""
import hmac
import hashlib
import json
import time
import logging
from typing import Optional, Dict, Any, Union

logger = logging.getLogger(__name__)


class StripeWebhookError(Exception):
    """Base exception for Stripe webhook processing."""
    pass


class StripeSignatureVerificationError(StripeWebhookError):
    """Raised when webhook signature verification fails."""
    pass


class MissingSignatureError(StripeSignatureVerificationError):
    """Raised when Stripe-Signature header is missing."""
    pass


class ExpiredSignatureError(StripeSignatureVerificationError):
    """Raised when webhook timestamp is outside tolerance."""
    pass


class InvalidSignatureError(StripeSignatureVerificationError):
    """Raised when webhook signature does not match payload."""
    pass


def generate_stripe_signature(
    payload: Union[str, bytes],
    secret: str,
    timestamp: Optional[int] = None
) -> str:
    """
    Generates a valid Stripe-Signature header format: t={timestamp},v1={hash}.
    Matches stripe.Webhook.generate_header specifications.
    """
    if timestamp is None:
        timestamp = int(time.time())

    payload_str = payload.decode('utf-8') if isinstance(payload, bytes) else payload
    signed_payload = f"{timestamp}.{payload_str}".encode('utf-8')
    computed_signature = hmac.new(
        secret.encode('utf-8'),
        signed_payload,
        hashlib.sha256
    ).hexdigest()

    return f"t={timestamp},v1={computed_signature}"


def verify_stripe_signature(
    payload: Union[str, bytes],
    sig_header: Optional[str],
    secret: str,
    tolerance: int = 300
) -> Dict[str, Any]:
    """
    Verifies the Stripe-Signature header against the raw request body.
    Implements Stripe's construct_event verification logic with timestamp tolerance
    and constant-time hash comparison (hmac.compare_digest).
    """
    if not sig_header:
        raise MissingSignatureError("Missing Stripe-Signature header.")

    if not secret:
        raise StripeSignatureVerificationError("Webhook secret is not configured.")

    # 1. Parse header components: t=..., v1=...
    header_parts = [part.strip() for part in sig_header.split(',') if '=' in part]
    timestamp: Optional[int] = None
    signatures = []

    for part in header_parts:
        key, value = part.split('=', 1)
        key = key.strip()
        value = value.strip()
        if key == 't':
            try:
                timestamp = int(value)
            except ValueError:
                raise InvalidSignatureError("Malformed timestamp in Stripe-Signature header.")
        elif key == 'v1':
            signatures.append(value)

    if timestamp is None:
        raise InvalidSignatureError("Timestamp 't' missing in Stripe-Signature header.")

    if not signatures:
        raise InvalidSignatureError("No 'v1' signature found in Stripe-Signature header.")

    # 2. Tolerance check (prevent replay attacks of stale events)
    current_time = int(time.time())
    if tolerance > 0 and (current_time - timestamp > tolerance):
        raise ExpiredSignatureError(
            f"Timestamp {timestamp} is outside tolerance zone ({tolerance}s). Current: {current_time}."
        )

    # 3. Compute expected signature over raw payload: "{timestamp}.{raw_payload}"
    payload_str = payload.decode('utf-8') if isinstance(payload, bytes) else payload
    signed_payload = f"{timestamp}.{payload_str}".encode('utf-8')
    expected_sig = hmac.new(
        secret.encode('utf-8'),
        signed_payload,
        hashlib.sha256
    ).hexdigest()

    # 4. Constant-time digest comparison to prevent timing attacks
    valid = any(hmac.compare_digest(expected_sig, sig) for sig in signatures)
    if not valid:
        raise InvalidSignatureError("Signature mismatch: payload or secret is invalid.")

    # 5. Parse and return JSON event payload
    try:
        event = json.loads(payload_str)
        if not isinstance(event, dict):
            raise StripeWebhookError("Webhook payload must be a JSON object.")
        return event
    except json.JSONDecodeError as exc:
        raise StripeWebhookError(f"Invalid JSON payload: {exc}")


class StripeWebhookService:
    """Namespace providing Stripe Webhook verification and utility methods."""

    generate_header = staticmethod(generate_stripe_signature)
    construct_event = staticmethod(verify_stripe_signature)

