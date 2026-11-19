"""Atomic Booking Management Service."""
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, Optional, List
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models.user import User
from app.models.court import Court
from app.models.coach import Coach
from app.models.equipment import Equipment
from app.models.booking import Booking, BookingSlot, BookingEquipment, BookingStatus
from app.services.pricing_service import PricingService
from app.services.availability_service import AvailabilityService

logger = logging.getLogger(__name__)


class BookingConflictError(Exception):
    """Raised when a requested resource (court, coach, or equipment) is unavailable."""
    pass


class BookingNotFoundError(Exception):
    """Raised when a requested booking record does not exist."""
    pass


class BookingService:
    """Orchestrates atomic reservation creation, validation, cancellation, and retrieval."""

    @classmethod
    def _insert_slot_on_conflict(
        cls,
        booking_id: int,
        court_id: int,
        coach_id: Optional[int],
        slot_date: date,
        time_slot: str,
        start_time_utc: datetime,
        end_time_utc: datetime
    ) -> bool:
        """
        Inserts a BookingSlot using ON CONFLICT DO NOTHING if supported.
        Returns False if a conflict occurred (0 rows inserted), True if inserted successfully.
        """
        dialect_name = db.engine.dialect.name
        slot_vals = {
            'booking_id': booking_id,
            'court_id': court_id,
            'coach_id': coach_id,
            'date': slot_date,
            'time_slot': time_slot,
            'start_time_utc': start_time_utc,
            'end_time_utc': end_time_utc
        }

        try:
            if dialect_name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                stmt = pg_insert(BookingSlot).values(**slot_vals).on_conflict_do_nothing(
                    constraint='uq_court_slot_start'
                )
                res = db.session.execute(stmt)
                return res.rowcount > 0
            elif dialect_name == 'sqlite':
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                stmt = sqlite_insert(BookingSlot).values(**slot_vals).on_conflict_do_nothing(
                    index_elements=['court_id', 'date', 'time_slot']
                )
                res = db.session.execute(stmt)
                return res.rowcount > 0
            else:
                booking_slot = BookingSlot(**slot_vals)
                db.session.add(booking_slot)
                db.session.flush()
                return True
        except IntegrityError:
            return False

    @classmethod
    def cleanup_stale_holds(cls) -> int:
        """
        Finds and releases stale holds that have exceeded their expiry.
        Physical slot locks are removed so availability is instantly restored.
        """
        now_utc = datetime.now(timezone.utc)
        stale_holds = Booking.query.filter(
            Booking.status == BookingStatus.HOLD.value,
            (
                ((Booking.hold_expires_at != None) & (Booking.hold_expires_at <= now_utc))
                | (Booking.created_at <= now_utc - timedelta(minutes=15))
            )
        ).all()

        count = len(stale_holds)
        for b in stale_holds:
            b.status = BookingStatus.CANCELLED.value
            BookingSlot.query.filter_by(booking_id=b.id).delete()

        if count > 0:
            db.session.commit()
            logger.info("Cleaned up %d stale booking holds", count)
        return count

    @classmethod
    def create_hold(
        cls,
        user_id: int,
        court_id: int,
        booking_date: date,
        time_slot: str,
        duration: int = 1,
        coach_id: Optional[int] = None,
        equipment_requests: Optional[Dict[str, int]] = None,
        client_timezone: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        hold_duration_minutes: int = 10
    ) -> Dict[str, Any]:
        """Atomically creates a short-lived hold on a court slot."""
        return cls.create_booking(
            user_id=user_id,
            court_id=court_id,
            booking_date=booking_date,
            time_slot=time_slot,
            duration=duration,
            coach_id=coach_id,
            equipment_requests=equipment_requests,
            client_timezone=client_timezone,
            idempotency_key=idempotency_key,
            status=BookingStatus.HOLD.value,
            hold_duration_minutes=hold_duration_minutes
        )

    @classmethod
    def confirm_booking(cls, booking_id: int, payment_intent_id: Optional[str] = None) -> Booking:
        """
        Confirms a held or pending booking outside of the reservation lock.
        Idempotent: if booking is already confirmed, returns without duplicate side effects.
        """
        booking = Booking.query.filter_by(id=booking_id).with_for_update().first()
        if not booking:
            raise BookingNotFoundError(f"Booking #{booking_id} does not exist.")

        if booking.status == BookingStatus.CONFIRMED.value:
            logger.info("Booking #%s is already confirmed (idempotent confirm)", booking_id)
            return booking

        if booking.status == BookingStatus.CANCELLED.value:
            raise ValueError(f"Cannot confirm cancelled booking #{booking_id}.")

        if booking.status == BookingStatus.HOLD.value and booking.hold_expires_at:
            now_utc = datetime.now(timezone.utc)
            expires_at = booking.hold_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now_utc:
                booking.status = BookingStatus.CANCELLED.value
                BookingSlot.query.filter_by(booking_id=booking.id).delete()
                db.session.commit()
                raise BookingConflictError(f"Hold for Booking #{booking_id} has expired.")

        booking.status = BookingStatus.CONFIRMED.value
        booking.hold_expires_at = None
        db.session.commit()
        logger.info("Confirmed Booking #%s outside resource lock", booking_id)
        return booking

    @classmethod
    def create_booking(
        cls,
        user_id: int,
        court_id: int,
        booking_date: date,
        time_slot: str,
        duration: int = 1,
        coach_id: Optional[int] = None,
        equipment_requests: Optional[Dict[str, int]] = None,
        client_timezone: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        status: Optional[str] = None,
        hold_duration_minutes: int = 10
    ) -> Dict[str, Any]:
        """
        Atomically creates a new court reservation or hold with narrow slot-level locking,
        database-level integrity guarantees, explicit UTC timestamp normalization, and server-side pricing.
        """
        # Idempotency check: if request was already processed, return existing booking safely
        if idempotency_key:
            existing_booking = Booking.query.filter_by(idempotency_key=idempotency_key).first()
            if existing_booking:
                logger.info("Idempotent hit for key %s (Booking #%s)", idempotency_key, existing_booking.id)
                return {
                    'booking_id': existing_booking.id,
                    'total_price': existing_booking.total_price,
                    'totalPrice': existing_booking.total_price,
                    'breakdown': [],
                    'applied_rules': [],
                    'booking': existing_booking.to_dict()
                }

        # Lazy cleanup of any stale holds before locking/allocating slots
        cls.cleanup_stale_holds()

        from app.utils.timezone import slot_to_utc_range, get_venue_timezone
        tz_to_use = client_timezone or str(get_venue_timezone())
        start_utc, end_utc = slot_to_utc_range(booking_date, time_slot, duration, tz_to_use)
        equipment_requests = equipment_requests or {}
        # Normalize equipment dictionary keys to int
        cleaned_equip: Dict[int, int] = {}
        for k, v in equipment_requests.items():
            try:
                k_int, v_int = int(k), int(v)
                if v_int > 0:
                    cleaned_equip[k_int] = v_int
            except (ValueError, TypeError):
                continue

        # 1. Fetch Court (read-only snapshot, NO row lock on the court table to avoid venue/court serialization)
        court = Court.query.filter_by(id=court_id, is_active=True).first()
        if not court:
            raise ValueError(f"Court #{court_id} does not exist or is currently inactive.")

        # 2. Fetch Coach (if requested, read-only snapshot)
        coach = None
        if coach_id:
            coach = Coach.query.filter_by(id=coach_id, is_active=True).first()
            if not coach:
                raise ValueError(f"Coach #{coach_id} does not exist or is currently inactive.")

        # 3. Check Duration and Operating Slot Bounds
        consecutive_slots = AvailabilityService.get_slots_for_duration(time_slot, duration)
        if len(consecutive_slots) < duration:
            raise ValueError(f"Cannot book {duration} hours starting at {time_slot} (exceeds operating hours).")

        # 4. Lock AT NARROWEST SCOPE: Only existing BookingSlot rows for this court and time window
        existing_court_slots = (
            BookingSlot.query.filter(
                BookingSlot.court_id == court_id,
                BookingSlot.date == booking_date,
                BookingSlot.time_slot.in_(consecutive_slots)
            )
            .with_for_update()
            .all()
        )
        if existing_court_slots or not AvailabilityService.is_court_available(court_id, booking_date, time_slot, duration):
            raise BookingConflictError(f"{court.name} is already booked for the selected time slot(s).")

        # 5. Lock existing Coach slots at narrow slot scope (if coach requested)
        if coach:
            existing_coach_slots = (
                BookingSlot.query.filter(
                    BookingSlot.coach_id == coach_id,
                    BookingSlot.date == booking_date,
                    BookingSlot.time_slot.in_(consecutive_slots)
                )
                .with_for_update()
                .all()
            )
            if existing_coach_slots or not AvailabilityService.is_coach_available(coach_id, booking_date, time_slot, duration):
                raise BookingConflictError(f"{coach.name} is already reserved for the selected time slot(s).")

        # 6. Lock Equipment rows and verify available inventory
        if cleaned_equip:
            locked_equipment = (
                Equipment.query.filter(Equipment.id.in_(list(cleaned_equip.keys())))
                .with_for_update()
                .all()
            )
            eq_avail_map = AvailabilityService.get_equipment_availability(booking_date, consecutive_slots)
            for eq_id, requested_qty in cleaned_equip.items():
                available_qty = eq_avail_map.get(eq_id, 0)
                if requested_qty > available_qty:
                    eq_obj = next((e for e in locked_equipment if e.id == eq_id), None) or db.session.get(Equipment, eq_id)
                    eq_name = eq_obj.name if eq_obj else f"Item #{eq_id}"
                    raise BookingConflictError(
                        f"Insufficient stock for {eq_name}. Requested: {requested_qty}, Available: {available_qty}"
                    )

        # 7. Authoritative Pricing Calculation
        price_result = PricingService.calculate_price(
            court=court,
            booking_date=booking_date,
            time_slot=time_slot,
            duration=duration,
            coach=coach,
            equipment_requests=cleaned_equip
        )
        total_price = price_result['total_price']

        # Determine target status and hold expiry
        target_status = status or BookingStatus.CONFIRMED.value
        hold_expires: Optional[datetime] = None
        if target_status == BookingStatus.HOLD.value:
            hold_expires = datetime.now(timezone.utc) + timedelta(minutes=hold_duration_minutes)

        # 8. Atomic Database Insertion with Slot Locks & Short Transaction
        try:
            booking = Booking(
                user_id=user_id,
                court_id=court_id,
                coach_id=coach_id,
                date=booking_date,
                time_slot=time_slot,
                duration=duration,
                total_price=total_price,
                status=target_status,
                start_time_utc=start_utc,
                end_time_utc=end_utc,
                timezone=tz_to_use,
                hold_expires_at=hold_expires,
                idempotency_key=idempotency_key
            )
            db.session.add(booking)
            db.session.flush()  # Flush to acquire booking.id
            booking_id = booking.id

            # Insert physical slot locks for each consecutive hour with UTC timestamps
            for slot in consecutive_slots:
                slot_start_utc, slot_end_utc = slot_to_utc_range(booking_date, slot, 1, tz_to_use)
                inserted = cls._insert_slot_on_conflict(
                    booking_id=booking_id,
                    court_id=court_id,
                    coach_id=coach_id,
                    slot_date=booking_date,
                    time_slot=slot,
                    start_time_utc=slot_start_utc,
                    end_time_utc=slot_end_utc
                )
                if not inserted:
                    db.session.rollback()
                    raise BookingConflictError(f"{court.name} is already booked for the selected time slot(s).")

            # Insert equipment line items
            for eq_id, qty in cleaned_equip.items():
                equip_item = BookingEquipment(
                    booking_id=booking_id,
                    equipment_id=eq_id,
                    quantity=qty
                )
                db.session.add(equip_item)

            booking_dict = booking.to_dict()
            db.session.commit()
            logger.info("Created Booking #%s (Status: %s) for User #%s (Total: %s)", booking_id, target_status, user_id, total_price)

            return {
                'booking_id': booking_id,
                'total_price': total_price,
                'totalPrice': total_price,
                'breakdown': price_result['breakdown'],
                'applied_rules': price_result['applied_rules'],
                'booking': booking_dict
            }

        except BookingConflictError:
            db.session.rollback()
            raise
        except IntegrityError as ie:
            db.session.rollback()
            logger.warning("Integrity error on booking creation: %s", ie)
            raise BookingConflictError("The court or coach was just booked by another user. Please choose another time slot.")
        except Exception as e:
            db.session.rollback()
            logger.exception("Unexpected error creating booking: %s", e)
            raise e

    @classmethod
    def cancel_booking(cls, booking_id: int, user_id: Optional[int] = None, is_admin: bool = False) -> Booking:
        """
        Cancels an active booking and immediately releases physical slot locks.
        Acquires row-level pessimistic lock (FOR UPDATE) to prevent cancellation race conditions.
        """
        booking = Booking.query.filter_by(id=booking_id).with_for_update().first()
        if not booking:
            raise BookingNotFoundError(f"Booking #{booking_id} does not exist.")

        if not is_admin and booking.user_id != user_id:
            raise PermissionError("You do not have permission to cancel this booking.")

        if booking.status == BookingStatus.CANCELLED.value:
            raise ValueError("This booking is already cancelled.")

        # Lock slots before deletion
        BookingSlot.query.filter_by(booking_id=booking.id).with_for_update().all()

        # Update status and delete physical slot locks to free up availability
        booking.status = BookingStatus.CANCELLED.value
        BookingSlot.query.filter_by(booking_id=booking.id).delete()

        db.session.commit()
        logger.info("Cancelled Booking #%s by User #%s (Admin: %s)", booking_id, user_id, is_admin)
        return booking

    @classmethod
    def complete_booking(cls, booking_id: int, is_admin: bool = False) -> Booking:
        """
        Marks a match booking as completed.
        """
        booking = db.session.get(Booking, booking_id)
        if not booking:
            raise BookingNotFoundError(f"Booking #{booking_id} does not exist.")

        if booking.status == BookingStatus.CANCELLED.value:
            raise ValueError("Cannot mark a cancelled booking as completed.")

        booking.status = BookingStatus.COMPLETED.value
        db.session.commit()
        logger.info("Completed Booking #%s", booking_id)
        return booking

    @classmethod
    def auto_complete_past_bookings(cls) -> int:
        """
        Automatically marks past confirmed bookings as completed using exact UTC timestamps.
        """
        from app.utils.timezone import get_current_utc_time, get_current_venue_date
        now_utc = get_current_utc_time()
        today_venue = get_current_venue_date()
        updated_count = 0

        # 1. Complete bookings where end_time_utc has elapsed
        utc_past_bookings = Booking.query.filter(
            Booking.end_time_utc != None,
            Booking.end_time_utc <= now_utc,
            Booking.status == BookingStatus.CONFIRMED.value
        ).all()
        for b in utc_past_bookings:
            b.status = BookingStatus.COMPLETED.value
            updated_count += 1

        # 2. Legacy rows where end_time_utc is null: compare against venue date
        legacy_past_bookings = Booking.query.filter(
            Booking.end_time_utc == None,
            Booking.date < today_venue,
            Booking.status == BookingStatus.CONFIRMED.value
        ).all()
        for b in legacy_past_bookings:
            b.status = BookingStatus.COMPLETED.value
            updated_count += 1

        if updated_count > 0:
            db.session.commit()
        return updated_count

