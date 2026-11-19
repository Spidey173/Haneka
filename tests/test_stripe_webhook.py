"""Stripe Webhook Signature Verification and Idempotent Replay Protection Test Suite."""
import json
import time
import pytest
from datetime import date, timedelta
from app.models import Court, Booking, BookingStatus, ProcessedWebhookEvent
from app.services.booking_service import BookingService
from app.services.stripe_service import generate_stripe_signature, verify_stripe_signature

TEST_WEBHOOK_SECRET = "whsec_test_secret_for_booking_system"


def test_stripe_signature_helper_generates_valid_format():
    """Verify test helper produces real signed payloads in t=...,v1=... format."""
    payload = json.dumps({"id": "evt_test_123", "type": "payment_intent.succeeded"})
    sig_header = generate_stripe_signature(payload, TEST_WEBHOOK_SECRET)

    assert sig_header.startswith("t=")
    assert ",v1=" in sig_header

    # Verify helper can verify its own generated signature
    event = verify_stripe_signature(payload, sig_header, TEST_WEBHOOK_SECRET)
    assert event["id"] == "evt_test_123"


def test_webhook_valid_signature_confirms_booking(client, app, seed_test_data, normal_user):
    """
    Verify sending a properly signed payload to /api/v1/webhooks/stripe:
    - Verifies raw payload bytes
    - Transitions booking from 'hold' to 'confirmed'
    - Records event ID in processed_webhook_events
    - Returns 200 OK
    """
    tomorrow = date.today() + timedelta(days=2)
    court = Court.query.first()

    # 1. Create a held booking
    hold_result = BookingService.create_hold(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot="10:00",
        duration=1
    )
    booking_id = hold_result["booking_id"]
    booking = Booking.query.get(booking_id)
    assert booking.status == BookingStatus.HOLD.value

    # 2. Build real webhook event
    event_id = "evt_test_success_valid_001"
    payload_dict = {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "metadata": {
                    "booking_id": str(booking_id)
                }
            }
        }
    }
    raw_payload = json.dumps(payload_dict)
    sig_header = generate_stripe_signature(raw_payload, TEST_WEBHOOK_SECRET)

    # 3. Post webhook with raw body bytes and signature header
    response = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={
            "Stripe-Signature": sig_header,
            "Content-Type": "application/json"
        }
    )

    assert response.status_code == 200
    res_data = response.get_json()
    assert res_data["status"] == "success"
    assert res_data["event_id"] == event_id

    # 4. Verify booking was confirmed
    updated_booking = Booking.query.get(booking_id)
    assert updated_booking.status == BookingStatus.CONFIRMED.value
    assert updated_booking.hold_expires_at is None

    # 5. Verify processed event recorded in database
    processed = ProcessedWebhookEvent.query.filter_by(event_id=event_id).first()
    assert processed is not None
    assert processed.event_type == "checkout.session.completed"


def test_webhook_invalid_signature_rejected(client, seed_test_data):
    """Verify webhook with wrong secret or tampered v1 signature returns 400."""
    raw_payload = json.dumps({"id": "evt_invalid_sig_001", "type": "payment_intent.succeeded"})
    wrong_sig_header = generate_stripe_signature(raw_payload, "whsec_completely_wrong_secret")

    response = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={"Stripe-Signature": wrong_sig_header}
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert "Signature mismatch" in data["error"]


def test_webhook_tampered_body_rejected(client, seed_test_data):
    """Verify webhook where body was tampered mid-flight returns 400."""
    original_payload = json.dumps({"id": "evt_original_001", "amount": 1000})
    sig_header = generate_stripe_signature(original_payload, TEST_WEBHOOK_SECRET)

    # Tampered body sent with original signature
    tampered_payload = json.dumps({"id": "evt_original_001", "amount": 10})

    response = client.post(
        "/api/v1/webhooks/stripe",
        data=tampered_payload,
        headers={"Stripe-Signature": sig_header}
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data
    assert "Signature mismatch" in data["error"]


def test_webhook_missing_header_rejected(client):
    """Verify webhook request without Stripe-Signature header returns 400."""
    raw_payload = json.dumps({"id": "evt_no_header_001", "type": "payment_intent.succeeded"})

    response = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "Missing Stripe-Signature header" in data["error"]


def test_webhook_expired_timestamp_outside_tolerance_rejected(client):
    """Verify webhook timestamp older than tolerance (300s) returns 400."""
    raw_payload = json.dumps({"id": "evt_expired_001", "type": "payment_intent.succeeded"})
    expired_timestamp = int(time.time()) - 600  # 10 minutes ago
    sig_header = generate_stripe_signature(raw_payload, TEST_WEBHOOK_SECRET, timestamp=expired_timestamp)

    response = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={"Stripe-Signature": sig_header}
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "tolerance zone" in data["error"]


def test_webhook_replay_protection_and_duplicate_delivery(client, seed_test_data, normal_user):
    """
    Verify replay/idempotency protection:
    - First delivery processes and confirms booking (returns 200)
    - Duplicate delivery safely ignored without double side effects (returns 200, status: ignored)
    - No duplicate confirmation or double update to booking occurs
    """
    tomorrow = date.today() + timedelta(days=3)
    court = Court.query.first()

    # 1. Create a hold
    hold_result = BookingService.create_hold(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot="15:00",
        duration=1
    )
    booking_id = hold_result["booking_id"]

    event_id = "evt_replay_test_999"
    raw_payload = json.dumps({
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test_999",
                "metadata": {"booking_id": str(booking_id)}
            }
        }
    })
    sig_header = generate_stripe_signature(raw_payload, TEST_WEBHOOK_SECRET)

    # First delivery: should process
    resp1 = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={"Stripe-Signature": sig_header}
    )
    assert resp1.status_code == 200
    assert resp1.get_json()["status"] == "success"

    booking_after_first = Booking.query.get(booking_id)
    assert booking_after_first.status == BookingStatus.CONFIRMED.value

    # Replayed / duplicate delivery: must return 200, status ignored, NO double side effects
    resp2 = client.post(
        "/api/v1/webhooks/stripe",
        data=raw_payload,
        headers={"Stripe-Signature": sig_header}
    )
    assert resp2.status_code == 200
    assert resp2.get_json()["status"] == "ignored"
    assert "already processed" in resp2.get_json()["message"]

    # Verify only 1 entry in processed_webhook_events
    events = ProcessedWebhookEvent.query.filter_by(event_id=event_id).all()
    assert len(events) == 1

    # Verify booking status remains confirmed and wasn't altered
    booking_after_second = Booking.query.get(booking_id)
    assert booking_after_second.status == BookingStatus.CONFIRMED.value

