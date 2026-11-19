import test from 'node:test';
import assert from 'node:assert/strict';
import type { BookingFlowState } from '../../src/types/index.js';

interface BookingMachineContext {
  state: BookingFlowState;
  pendingSlot: string | null;
  confirmedSlot: string | null;
  history: string[];
}

class BookingStateMachine {
  context: BookingMachineContext;

  constructor() {
    this.context = {
      state: 'idle',
      pendingSlot: null,
      confirmedSlot: null,
      history: ['idle'],
    };
  }

  get isSlotDisabled(): boolean {
    return this.context.state === 'pending' || this.context.state === 'unknown';
  }

  get isButtonDisabled(): boolean {
    return this.context.state === 'pending' || this.context.state === 'unknown';
  }

  getSlotDisplay(slot: string): string {
    if (this.context.pendingSlot === slot) {
      if (this.context.state === 'unknown') {
        return 'Reconciling...';
      }
      if (this.context.state === 'pending') {
        return 'Confirming...';
      }
    }
    if (this.context.confirmedSlot === slot) {
      return 'Confirmed';
    }
    return 'Available';
  }

  async submitBooking(
    slot: string,
    courtId: number,
    date: string,
    apiClient: {
      createBooking: (payload: any) => Promise<any>;
      reconcileBooking: (key: string, courtId: number, date: string, slot: string) => Promise<{ confirmed: boolean; booking?: any }>;
    }
  ) {
    if (this.context.state === 'pending' || this.context.state === 'unknown') {
      return; // Prevent double submits
    }

    const idempotencyKey = `idem-${Date.now()}`;

    // 1. Move to pending state
    this.context.state = 'pending';
    this.context.pendingSlot = slot;
    this.context.history.push('pending');

    try {
      await apiClient.createBooking({
        court_id: courtId,
        date,
        time_slot: slot,
        idempotency_key: idempotencyKey,
      });

      // 2. Server ACK: move to confirmed
      this.context.state = 'confirmed';
      this.context.confirmedSlot = slot;
      this.context.pendingSlot = null;
      this.context.history.push('confirmed');
    } catch (err: any) {
      if (err.status && err.status >= 400 && err.status < 500) {
        // Business rejection
        this.context.state = 'failed';
        this.context.pendingSlot = null;
        this.context.history.push('failed');
      } else {
        // Mid-flight network drop / timeout!
        // DO NOT show confirmed; transition to unknown (reconciling)
        this.context.state = 'unknown';
        this.context.history.push('unknown');

        // Reconcile server state
        const rec = await apiClient.reconcileBooking(idempotencyKey, courtId, date, slot);
        if (rec.confirmed) {
          this.context.state = 'confirmed';
          this.context.confirmedSlot = slot;
          this.context.pendingSlot = null;
          this.context.history.push('confirmed');
        } else {
          this.context.state = 'failed';
          this.context.pendingSlot = null;
          this.context.history.push('failed');
        }
      }
    }
  }
}

test('UI never shows confirmed optimistically during in-flight submission', async () => {
  const machine = new BookingStateMachine();
  assert.equal(machine.context.state, 'idle');
  assert.equal(machine.getSlotDisplay('10:00'), 'Available');

  // Simulated pending request with delayed promise
  let resolvePromise: (val: any) => void;
  const mockApi = {
    createBooking: () =>
      new Promise((resolve) => {
        resolvePromise = resolve;
      }),
    reconcileBooking: async () => ({ confirmed: false }),
  };

  const submitPromise = machine.submitBooking('10:00', 1, '2026-11-21', mockApi);

  // While in flight:
  assert.equal(machine.context.state, 'pending');
  assert.equal(machine.getSlotDisplay('10:00'), 'Confirming...');
  assert.notEqual(machine.getSlotDisplay('10:00'), 'Confirmed');
  assert.equal(machine.isSlotDisabled, true);
  assert.equal(machine.isButtonDisabled, true);

  // Complete request
  resolvePromise!({ success: true, booking_id: 123 });
  await submitPromise;

  // Only now shows confirmed
  assert.equal(machine.context.state, 'confirmed');
  assert.equal(machine.getSlotDisplay('10:00'), 'Confirmed');
  assert.deepEqual(machine.context.history, ['idle', 'pending', 'confirmed']);
});

test('Request fails mid-flight, UI never shows confirmed, and reconciles to confirmed if server committed', async () => {
  const machine = new BookingStateMachine();

  // Simulated network drop mid-flight (e.g. response failed to reach client)
  const mockApi = {
    createBooking: async () => {
      const netErr = new Error('Failed to fetch: NetworkError');
      // No err.status (network drop)
      throw netErr;
    },
    reconcileBooking: async (key: string, courtId: number, date: string, slot: string) => {
      // Reconcile check finds server actually processed the booking
      return {
        confirmed: true,
        booking: { id: 99, court_id: courtId, date, time_slot: slot, status: 'confirmed' },
      };
    },
  };

  await machine.submitBooking('14:00', 2, '2026-11-21', mockApi);

  // History must have transitioned: idle -> pending -> unknown -> confirmed
  assert.deepEqual(machine.context.history, ['idle', 'pending', 'unknown', 'confirmed']);
  assert.equal(machine.context.state, 'confirmed');
  assert.equal(machine.getSlotDisplay('14:00'), 'Confirmed');
});

test('Request fails mid-flight, UI never shows confirmed, and rolls back cleanly if server never committed', async () => {
  const machine = new BookingStateMachine();

  // Simulated network drop mid-flight where server never received payload
  const mockApi = {
    createBooking: async () => {
      const netErr = new Error('TypeError: network error');
      throw netErr;
    },
    reconcileBooking: async () => {
      // Reconcile check confirms server has no record
      return { confirmed: false };
    },
  };

  await machine.submitBooking('16:00', 3, '2026-11-21', mockApi);

  // History must have transitioned: idle -> pending -> unknown -> failed
  assert.deepEqual(machine.context.history, ['idle', 'pending', 'unknown', 'failed']);
  assert.equal(machine.context.state, 'failed');
  assert.equal(machine.context.pendingSlot, null);
  assert.equal(machine.context.confirmedSlot, null);
  assert.equal(machine.getSlotDisplay('16:00'), 'Available');
  // At no point in history did 'confirmed' appear!
  assert.equal(machine.context.history.includes('confirmed'), false);
});

