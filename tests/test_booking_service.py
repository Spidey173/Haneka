"""Booking Transaction & Concurrency Test Suite."""
import pytest
from datetime import date, timedelta
from app.models import Court, Coach, Equipment, Booking, BookingSlot, BookingStatus
from app.services.booking_service import BookingService, BookingConflictError


def test_create_single_hour_booking(seed_test_data, normal_user):
    """Verify standard 1-hour booking creation."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(type='indoor').first()

    result = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='10:00',
        duration=1
    )
    assert result['booking_id'] is not None
    assert result['total_price'] > 0

    # Verify physical slot lock in DB
    slot = BookingSlot.query.filter_by(court_id=court.id, date=tomorrow, time_slot='10:00').first()
    assert slot is not None
    assert slot.booking_id == result['booking_id']


def test_create_multi_hour_booking(seed_test_data, normal_user):
    """Verify multi-hour booking creates consecutive physical slot locks."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(type='indoor').first()

    result = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='08:00',
        duration=3
    )
    assert result['booking_id'] is not None

    # Verify all 3 slots (08:00, 09:00, 10:00) are locked
    slots = BookingSlot.query.filter_by(court_id=court.id, date=tomorrow).order_by(BookingSlot.time_slot).all()
    assert len(slots) == 3
    assert [s.time_slot for s in slots] == ['08:00', '09:00', '10:00']


def test_double_booking_prevention_same_slot(seed_test_data, normal_user):
    """Verify attempting to book the exact same slot raises BookingConflictError."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(type='indoor').first()

    # User 1 books
    BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='14:00',
        duration=1
    )

    # User 2 attempts same slot
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='14:00',
            duration=1
        )


def test_double_booking_prevention_overlapping_multi_hour(seed_test_data, normal_user):
    """Verify booking overlapping with an active multi-hour reservation is blocked."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(type='indoor').first()

    # User 1 books 08:00 to 11:00 (duration=3)
    BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='08:00',
        duration=3
    )

    # User 2 attempts 09:00 (1 hour)
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='09:00',
            duration=1
        )

    # User 2 attempts 10:00 (2 hours: 10:00 and 11:00)
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='10:00',
            duration=2
        )


def test_coach_double_booking_prevention(seed_test_data, normal_user):
    """Verify same coach cannot be assigned to two different courts simultaneously."""
    tomorrow = date.today() + timedelta(days=1)
    court1 = Court.query.filter_by(id=1).first()
    court2 = Court.query.filter_by(id=2).first()
    coach = Coach.query.first()

    # Booking on Court 1 with Coach
    BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court1.id,
        booking_date=tomorrow,
        time_slot='11:00',
        duration=1,
        coach_id=coach.id
    )

    # Attempt booking on Court 2 with same Coach at same time -> blocked
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court2.id,
            booking_date=tomorrow,
            time_slot='11:00',
            duration=1,
            coach_id=coach.id
        )


def test_equipment_inventory_exhaustion(seed_test_data, normal_user):
    """Verify requesting more equipment than available stock raises error."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(id=1).first()
    racket = Equipment.query.filter_by(name='Badminton Racket').first()
    assert racket.total_available == 10

    # Requesting 15 rackets (more than 10 available)
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='12:00',
            duration=1,
            equipment_requests={racket.id: 15}
        )


def test_booking_cancellation_releases_slot(seed_test_data, normal_user):
    """Verify cancelling a booking immediately frees up the slot for other users."""
    tomorrow = date.today() + timedelta(days=1)
    court = Court.query.filter_by(id=1).first()

    # Create booking
    result = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='16:00',
        duration=1
    )
    booking_id = result['booking_id']

    # Cancel booking
    BookingService.cancel_booking(booking_id=booking_id, user_id=normal_user.id)

    booking = Booking.query.get(booking_id)
    assert booking.status == BookingStatus.CANCELLED.value

    # Check slot lock is removed
    slot = BookingSlot.query.filter_by(booking_id=booking_id).first()
    assert slot is None

    # Now someone else can book that exact slot successfully
    new_result = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='16:00',
        duration=1
    )
    assert new_result['booking_id'] is not None


def test_row_locking_concurrent_slot_conflict_prevention(seed_test_data, normal_user):
    """Verify that pessimistic row-level locking (with_for_update) detects existing slot locks."""
    tomorrow = date.today() + timedelta(days=2)
    court = Court.query.filter_by(id=1).first()

    # Initial booking acquires row lock and writes slots
    res1 = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='18:00',
        duration=2
    )
    assert res1['booking_id'] is not None

    # Concurrent attempt on overlapping slot 19:00 fails via locked slot check
    with pytest.raises(BookingConflictError) as exc_info:
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='19:00',
            duration=1
        )
    assert "already booked" in str(exc_info.value)


def test_concurrent_same_slot_exactly_one_succeeds(app, seed_test_data, normal_user):
    """
    Concurrency test: N parallel requests for the exact same slot -> exactly 1 succeeds,
    the rest get 409 Conflict.
    """
    from concurrent.futures import ThreadPoolExecutor
    tomorrow = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    court_id = int(seed_test_data['courts'][0].id)
    user_id = int(normal_user.id)
    concurrency_count = 10

    def make_booking_request(i):
        # Each thread gets its own test client logged in as normal_user
        with app.app_context():
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(user_id)
                sess['_fresh'] = True
            response = client.post('/api/bookings/create', json={
                'court_id': court_id,
                'date': tomorrow,
                'time_slot': '10:00',
                'duration': 1,
                'idempotency_key': f'req-concurrent-{i}'
            })
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=concurrency_count) as executor:
        results = list(executor.map(make_booking_request, range(concurrency_count)))

    status_codes = [r[0] for r in results]
    successes = status_codes.count(201)
    conflicts = status_codes.count(409)

    assert successes == 1, f"Expected exactly 1 booking to succeed, but got {successes}. Codes: {status_codes}"
    assert conflicts == concurrency_count - 1, f"Expected {concurrency_count - 1} requests to get 409, got {conflicts}"


def test_concurrent_different_courts_do_not_block_each_other(app, seed_test_data, normal_user):
    """
    Concurrency test: Parallel requests for different courts at the same venue
    do not block each other and all succeed.
    """
    from concurrent.futures import ThreadPoolExecutor
    tomorrow = (date.today() + timedelta(days=4)).strftime('%Y-%m-%d')
    court_ids = [int(c.id) for c in seed_test_data['courts']]
    user_id = int(normal_user.id)
    assert len(court_ids) >= 3

    def book_court(cid):
        with app.app_context():
            client = app.test_client()
            with client.session_transaction() as sess:
                sess['_user_id'] = str(user_id)
                sess['_fresh'] = True
            response = client.post('/api/bookings/create', json={
                'court_id': cid,
                'date': tomorrow,
                'time_slot': '14:00',
                'duration': 1,
                'idempotency_key': f'court-{cid}-slot-14'
            })
            return response.status_code

    with ThreadPoolExecutor(max_workers=len(court_ids)) as executor:
        status_codes = list(executor.map(book_court, court_ids))

    assert all(code == 201 for code in status_codes), f"All different courts should succeed. Got: {status_codes}"


def test_short_lived_hold_and_lazy_cleanup(seed_test_data, normal_user):
    """
    Verify short-lived hold creation, conflict during active hold,
    and automatic release after hold expires via lazy cleanup.
    """
    from datetime import datetime, timezone
    tomorrow = date.today() + timedelta(days=5)
    court = Court.query.filter_by(id=1).first()

    # 1. Create a hold with 10-minute expiry
    hold_result = BookingService.create_hold(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='11:00',
        duration=1,
        hold_duration_minutes=10
    )
    booking_id = hold_result['booking_id']
    booking = Booking.query.get(booking_id)
    assert booking.status == BookingStatus.HOLD.value
    assert booking.hold_expires_at is not None

    # Slot lock exists
    slot = BookingSlot.query.filter_by(booking_id=booking_id).first()
    assert slot is not None

    # 2. Concurrent booking attempt while hold is active fails with 409
    with pytest.raises(BookingConflictError):
        BookingService.create_booking(
            user_id=normal_user.id,
            court_id=court.id,
            booking_date=tomorrow,
            time_slot='11:00',
            duration=1
        )

    # 3. Simulate hold expiration by setting hold_expires_at in the past
    booking.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.extensions import db
    db.session.commit()

    # 4. New booking attempt triggers lazy cleanup and acquires the now-free slot
    new_result = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='11:00',
        duration=1
    )
    assert new_result['booking_id'] is not None
    assert new_result['booking_id'] != booking_id

    # Old booking is marked cancelled
    old_booking = Booking.query.get(booking_id)
    assert old_booking.status == BookingStatus.CANCELLED.value


def test_idempotency_key_replay_protection(seed_test_data, normal_user):
    """
    Verify that repeating a booking request with the same idempotency key
    safely returns the existing booking without double-booking or raising conflicts.
    """
    tomorrow = date.today() + timedelta(days=6)
    court = Court.query.filter_by(id=1).first()
    key = "idem-test-key-safe-retry-123"

    res1 = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='09:00',
        duration=1,
        idempotency_key=key
    )
    b_id1 = res1['booking_id']

    # Retry with identical idempotency key
    res2 = BookingService.create_booking(
        user_id=normal_user.id,
        court_id=court.id,
        booking_date=tomorrow,
        time_slot='09:00',
        duration=1,
        idempotency_key=key
    )
    b_id2 = res2['booking_id']

    assert b_id1 == b_id2
    assert res1['total_price'] == res2['total_price']
    # Ensure only 1 booking slot exists
    slots = BookingSlot.query.filter_by(court_id=court.id, date=tomorrow, time_slot='09:00').all()
    assert len(slots) == 1


