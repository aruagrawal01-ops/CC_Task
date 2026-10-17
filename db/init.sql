-- The Last Seat: schema. The seats row is the single source of truth for who owns a seat right now.

CREATE TABLE seats (
    id              INT PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'held', 'booked')),
    held_by         TEXT,
    reservation_id  BIGINT,
    hold_expires_at TIMESTAMPTZ,
    -- an available seat points at nobody; a held/booked seat always points at a reservation
    CHECK ((status = 'available') = (reservation_id IS NULL AND held_by IS NULL)),
    CHECK ((status = 'held') = (hold_expires_at IS NOT NULL))
);

CREATE TABLE reservations (
    id         BIGSERIAL PRIMARY KEY,
    seat_id    INT NOT NULL REFERENCES seats(id),
    user_id    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('held', 'confirmed', 'cancelled', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Backstop: even if the application logic were wrong, the DB refuses a second live reservation per seat.
CREATE UNIQUE INDEX one_active_reservation_per_seat ON reservations (seat_id) WHERE status IN ('held', 'confirmed');
-- Fairness: one live seat per student.
CREATE UNIQUE INDEX one_active_reservation_per_user ON reservations (user_id) WHERE status IN ('held', 'confirmed');

-- seats -> reservations is circular (the seat row is written before the reservation row in the same transaction)
ALTER TABLE seats ADD CONSTRAINT seats_reservation_fk
    FOREIGN KEY (reservation_id) REFERENCES reservations(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE payments (
    id             BIGSERIAL PRIMARY KEY,
    reservation_id BIGINT NOT NULL REFERENCES reservations(id),
    mode           TEXT NOT NULL CHECK (mode IN ('success', 'fail', 'delayed')),
    status         TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'void', 'refunded')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one payment in flight or captured per reservation: no double charge.
CREATE UNIQUE INDEX one_live_payment_per_reservation ON payments (reservation_id) WHERE status IN ('pending', 'succeeded');

CREATE TABLE idempotency_keys (
    user_id      TEXT NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code  INT,           -- NULL while the first request is still in progress
    response     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);

-- Sweeper scans only live holds.
CREATE INDEX seats_held_expiry ON seats (hold_expires_at) WHERE status = 'held';

-- 500 seats: rows A..T, 25 seats each.
INSERT INTO seats (id, label)
SELECT g, chr(65 + (g - 1) / 25) || ((g - 1) % 25 + 1)
FROM generate_series(1, 500) AS g;
