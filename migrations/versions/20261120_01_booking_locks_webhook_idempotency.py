"""booking_locks_webhook_idempotency

Revision ID: 20261120_01
Revises: None
Create Date: 2026-11-20 01:50:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20261120_01'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add hold_expires_at and idempotency_key to bookings table
    op.add_column('bookings', sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('bookings', sa.Column('idempotency_key', sa.String(length=255), nullable=True))
    op.create_index('idx_bookings_hold_expiry', 'bookings', ['status', 'hold_expires_at'], unique=False)
    op.create_index('idx_bookings_idempotency', 'bookings', ['idempotency_key'], unique=True)

    # 2. Add slot-level start_time_utc unique constraint to booking_slots
    op.create_unique_constraint('uq_court_slot_start', 'booking_slots', ['court_id', 'start_time_utc'])

    # 3. Create processed_webhook_events table for Stripe replay protection
    op.create_table(
        'processed_webhook_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.String(length=255), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=False)
    )
    op.create_index('idx_processed_webhook_events_event_id', 'processed_webhook_events', ['event_id'], unique=True)


def downgrade():
    op.drop_index('idx_processed_webhook_events_event_id', table_name='processed_webhook_events')
    op.drop_table('processed_webhook_events')
    op.drop_constraint('uq_court_slot_start', 'booking_slots', type_='unique')
    op.drop_index('idx_bookings_idempotency', table_name='bookings')
    op.drop_index('idx_bookings_hold_expiry', table_name='bookings')
    op.drop_column('bookings', 'idempotency_key')
    op.drop_column('bookings', 'hold_expires_at')

