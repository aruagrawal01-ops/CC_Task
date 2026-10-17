"""Booking logic. Every write locks the seat row first, then reservations, then payments (fixed order = no deadlocks)."""
import os
import time

from psycopg import errors

from . import db

HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "120"))
PAYMENT_DELAY_SECONDS = float(os.environ.get("PAYMENT_DELAY_SECONDS", "3"))
PAYMENT_MODES = ("success", "fail", "delayed")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _res(r):
    return {
        "id": r["id"],
        "seat_id": r["seat_id"],
        "user_id": r["user_id"],
        "status": r["status"],
        "expires_at": r["expires_at"].isoformat(),
        "created_at": r["created_at"].isoformat(),
    }


# ---------- reads ----------

def list_seats():
    with db.conn() as c:
        rows = c.execute("""
            SELECT id, label,
                   CASE WHEN status = 'held' AND hold_expires_at < now() THEN 'available' ELSE status END AS status
            FROM seats ORDER BY id""").fetchall()
    return rows


def get_seat(seat_id):
    with db.conn() as c:
        row = c.execute("""
            SELECT id, label,
                   CASE WHEN status = 'held' AND hold_expires_at < now() THEN 'available' ELSE status END AS status
            FROM seats WHERE id = %s""", (seat_id,)).fetchone()
    if not row:
        raise ApiError(404, "seat not found")
    return row


def get_reservation(user_id, rid):
    with db.conn() as c:
        r = c.execute("SELECT * FROM reservations WHERE id = %s AND user_id = %s", (rid, user_id)).fetchone()
    if not r:
        raise ApiError(404, "reservation not found")  # 404, not 403: don't reveal other users' reservations
    return _res(r)


def my_bookings(user_id):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM reservations WHERE user_id = %s ORDER BY id DESC", (user_id,)).fetchall()
    return [_res(r) for r in rows]


# ---------- reserve ----------

def reserve(user_id, seat_id, _retry=True):
    try:
        with db.conn() as c, c.transaction():
            rid = c.execute("SELECT nextval('reservations_id_seq') AS id").fetchone()["id"]
            # The referee: check and act in ONE statement. A concurrent UPDATE on the same row waits for our
            # commit, then re-evaluates this WHERE against the new row version and matches 0 rows.
            won = c.execute("""
                UPDATE seats
                SET status = 'held', held_by = %(u)s, reservation_id = %(rid)s,
                    hold_expires_at = now() + make_interval(secs => %(ttl)s)
                WHERE id = %(s)s
                  AND (status = 'available' OR (status = 'held' AND hold_expires_at < now()))
                RETURNING hold_expires_at""",
                {"u": user_id, "rid": rid, "s": seat_id, "ttl": HOLD_SECONDS}).fetchone()
            if won:
                # Lazy expiry may have just taken over someone's stale hold; retire it. We hold the seat lock,
                # so nobody else can be touching this seat's reservations.
                c.execute("UPDATE reservations SET status = 'expired', updated_at = now() "
                          "WHERE seat_id = %s AND status = 'held'", (seat_id,))
                r = c.execute("""
                    INSERT INTO reservations (id, seat_id, user_id, status, expires_at)
                    VALUES (%s, %s, %s, 'held', %s) RETURNING *""",
                    (rid, seat_id, user_id, won["hold_expires_at"])).fetchone()
                return 201, _res(r)
    except errors.UniqueViolation as e:
        if e.diag.constraint_name == "one_active_reservation_per_user":
            # Their old hold may be expired-but-not-yet-swept; clear it and try once more.
            if _retry and sweep(user_id=user_id):
                return reserve(user_id, seat_id, _retry=False)
            raise ApiError(409, "you already hold or own a seat")
        raise ApiError(409, "seat is not available")  # backstop index fired

    # Lost the race (or seat taken). Natural idempotency: if it's already ours, return our reservation.
    with db.conn() as c:
        seat = c.execute("SELECT * FROM seats WHERE id = %s", (seat_id,)).fetchone()
        if not seat:
            raise ApiError(404, "seat not found")
        if seat["held_by"] == user_id:
            r = c.execute("SELECT * FROM reservations WHERE id = %s", (seat["reservation_id"],)).fetchone()
            return 200, _res(r)
    raise ApiError(409, "seat is not available")


# ---------- pay ----------

def pay(user_id, rid, mode):
    if mode not in PAYMENT_MODES:
        raise ApiError(422, f"mode must be one of {', '.join(PAYMENT_MODES)}")

    # Step 1: verify the hold is live and record a pending payment. Short transaction.
    with db.conn() as c, c.transaction():
        r = c.execute("""
            SELECT r.*, (s.reservation_id = r.id AND s.status = 'held' AND s.hold_expires_at >= now()) AS hold_live
            FROM reservations r JOIN seats s ON s.id = r.seat_id
            WHERE r.id = %s AND r.user_id = %s""", (rid, user_id)).fetchone()
        if not r:
            raise ApiError(404, "reservation not found")
        if r["status"] == "confirmed":
            return 200, _res(r)  # already paid: repeatable
        if r["status"] != "held" or not r["hold_live"]:
            raise ApiError(410, "hold has expired or was cancelled")
        try:
            with c.transaction():  # savepoint so a unique violation doesn't poison the outer txn
                pid = c.execute("INSERT INTO payments (reservation_id, mode, status) VALUES (%s, %s, 'pending') "
                                "RETURNING id", (rid, mode)).fetchone()["id"]
        except errors.UniqueViolation:
            raise ApiError(409, "a payment for this reservation is already in progress")

    # Step 2: talk to the "gateway" with NO transaction and NO pooled connection held.
    if mode == "delayed":
        time.sleep(PAYMENT_DELAY_SECONDS)
    approved = mode != "fail"

    # Step 3: record the outcome. Seat row first; the guard makes a late/stale confirm a no-op.
    with db.conn() as c, c.transaction():
        if not approved:
            c.execute("UPDATE payments SET status = 'failed', updated_at = now() WHERE id = %s", (pid,))
            booked = False
        else:
            booked = c.execute("""
                UPDATE seats SET status = 'booked', hold_expires_at = NULL
                WHERE id = %s AND reservation_id = %s AND status = 'held' AND hold_expires_at >= now()""",
                (r["seat_id"], rid)).rowcount == 1
            if booked:
                r = c.execute("UPDATE reservations SET status = 'confirmed', updated_at = now() "
                              "WHERE id = %s RETURNING *", (rid,)).fetchone()
            c.execute("UPDATE payments SET status = %s, updated_at = now() WHERE id = %s",
                      ("succeeded" if booked else "void", pid))

    if not approved:
        raise ApiError(402, "payment declined; your hold is kept until it expires, you may retry")
    if not booked:
        raise ApiError(409, "hold expired before payment completed; payment voided")
    return 200, _res(r)


# ---------- cancel ----------

def cancel(user_id, rid):
    with db.conn() as c, c.transaction():
        # No FOR UPDATE here: locking the reservation before the seat would break the lock order.
        r = c.execute("SELECT * FROM reservations WHERE id = %s AND user_id = %s", (rid, user_id)).fetchone()
        if not r:
            raise ApiError(404, "reservation not found")
        if r["status"] in ("cancelled", "expired"):
            return 200, _res(r)  # repeatable
        # Guard on reservation_id: a stale cancel can never free a seat someone else now holds.
        freed = c.execute("""
            UPDATE seats SET status = 'available', held_by = NULL, reservation_id = NULL, hold_expires_at = NULL
            WHERE id = %s AND reservation_id = %s""", (r["seat_id"], rid)).rowcount == 1
        r = c.execute("""
            UPDATE reservations SET status = %s, updated_at = now()
            WHERE id = %s AND status IN ('held', 'confirmed') RETURNING *""",
            ("cancelled" if freed else "expired", rid)).fetchone() or r
        if freed:
            c.execute("UPDATE payments SET status = 'refunded', updated_at = now() "
                      "WHERE reservation_id = %s AND status = 'succeeded'", (rid,))
    return 200, _res(r)


# ---------- expiry ----------

SWEEP_SQL = """
WITH expired AS (
    SELECT id, reservation_id FROM seats
    WHERE status = 'held' AND hold_expires_at < now()
      AND (%(u)s::text IS NULL OR held_by = %(u)s)
    ORDER BY id
    LIMIT %(batch)s
    FOR UPDATE SKIP LOCKED          -- don't wait on seats a request is busy with; get them next round
), freed AS (
    UPDATE seats s
    SET status = 'available', held_by = NULL, reservation_id = NULL, hold_expires_at = NULL
    FROM expired e WHERE s.id = e.id
    RETURNING e.reservation_id
)
UPDATE reservations r SET status = 'expired', updated_at = now()
FROM freed f WHERE r.id = f.reservation_id AND r.status = 'held'
"""


def sweep(user_id=None, batch=500):
    """Eagerly free expired holds. Returns how many reservations were expired."""
    with db.conn() as c:
        return c.execute(SWEEP_SQL, {"u": user_id, "batch": batch}).rowcount
