-- The Last Seat: database setup.
-- WARNING: running this file deletes all existing data and starts fresh.

DROP TABLE IF EXISTS payments, reservations, seats;

-- One row per seat. This row decides who owns the seat right now.
CREATE TABLE seats (
    id              INT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'available',  -- available | held | booked
    reservation_id  INT,                                -- which reservation owns this seat
    hold_expires_at TIMESTAMPTZ                         -- when the hold runs out (only for 'held')
);

-- Every reservation ever made (this is the booking history).
CREATE TABLE reservations (
    id         SERIAL PRIMARY KEY,
    seat_id    INT NOT NULL REFERENCES seats(id),
    user_id    TEXT NOT NULL,
    status     TEXT NOT NULL,                 -- held | confirmed | cancelled | expired
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every payment attempt.
CREATE TABLE payments (
    id             SERIAL PRIMARY KEY,
    reservation_id INT NOT NULL REFERENCES reservations(id),
    mode           TEXT NOT NULL,             -- success | fail | delayed
    status         TEXT NOT NULL,             -- succeeded | failed | too_late | refunded
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Create the 500 seats.
INSERT INTO seats (id) SELECT generate_series(1, 500);
