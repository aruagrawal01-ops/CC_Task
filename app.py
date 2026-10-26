"""The Last Seat: a concert ticket booking backend (500 seats, many students booking at once)."""
import os
import time

from flask import Flask, jsonify, request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/lastseat")
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "120"))                    # how long a seat stays held without paying
PAYMENT_DELAY_SECONDS = float(os.environ.get("PAYMENT_DELAY_SECONDS", "3"))  # how slow a "delayed" payment is
TOTAL_SEATS = 500

app = Flask(__name__)

# A pool keeps up to 10 database connections open and lends them to requests,
# so we don't open a new connection (slow) for every request.
pool = ConnectionPool(DATABASE_URL, max_size=10, kwargs={"row_factory": dict_row}, open=True)

# Columns we return for a reservation. A hold whose time has run out is reported as 'expired'.
RESERVATION_COLUMNS = """id, seat_id, user_id, created_at, expires_at,
    CASE WHEN status = 'held' AND expires_at < now() THEN 'expired' ELSE status END AS status"""


def error(status, message):
    return jsonify(error=message), status


def current_user():
    # Shortcut for login: the client sends its user id in the X-User-Id header.
    user = request.headers.get("X-User-Id", "").strip()
    return user if 0 < len(user) <= 64 else None


def find_reservation(conn, reservation_id, user):
    # Users can only see their own reservations.
    return conn.execute(f"SELECT {RESERVATION_COLUMNS} FROM reservations WHERE id = %s AND user_id = %s",
                        (reservation_id, user)).fetchone()


@app.get("/seats")
def list_seats():
    with pool.connection() as conn:
        seats = conn.execute("""
            SELECT id,
                   CASE WHEN status = 'held' AND hold_expires_at < now() THEN 'available' ELSE status END AS status
            FROM seats ORDER BY id""").fetchall()
    available = sum(1 for s in seats if s["status"] == "available")
    return jsonify(available=available, seats=seats)


@app.post("/reservations")
def reserve_seat():
    user = current_user()
    if not user:
        return error(401, "send your user id in the X-User-Id header")
    seat_id = (request.get_json(silent=True) or {}).get("seat_id")
    if type(seat_id) is not int:
        return error(400, "seat_id must be a number")
    if not 1 <= seat_id <= TOTAL_SEATS:
        return error(404, "seat not found")

    with pool.connection() as conn:
        # Step 1: write the reservation row (not saved for real until the end of this block).
        reservation = conn.execute(f"""
            INSERT INTO reservations (seat_id, user_id, status, expires_at)
            VALUES (%s, %s, 'held', now() + make_interval(secs => %s))
            RETURNING {RESERVATION_COLUMNS}""", (seat_id, user, HOLD_SECONDS)).fetchone()

        # Step 2: THE KEY STEP. Take the seat only if it is free, or if the previous hold has run out.
        # The check and the update are ONE statement. Postgres locks the seat row, so if two people
        # try at the same time, the second one waits, re-checks, finds the seat taken, and updates 0 rows.
        won = conn.execute("""
            UPDATE seats SET status = 'held', reservation_id = %s, hold_expires_at = %s
            WHERE id = %s AND (status = 'available' OR (status = 'held' AND hold_expires_at < now()))""",
            (reservation["id"], reservation["expires_at"], seat_id)).rowcount == 1

        if won:
            # If we took over a hold that had run out, mark that old reservation as expired.
            conn.execute("UPDATE reservations SET status = 'expired' WHERE seat_id = %s AND status = 'held' AND id <> %s",
                         (seat_id, reservation["id"]))
            return jsonify(reservation), 201  # leaving the 'with' block saves (commits) everything

        # We lost: undo step 1.
        conn.rollback()

        # Repeated request: if this user already holds or owns the seat, give back their reservation.
        mine = conn.execute(f"""
            SELECT {RESERVATION_COLUMNS} FROM reservations
            WHERE id = (SELECT reservation_id FROM seats WHERE id = %s) AND user_id = %s""",
            (seat_id, user)).fetchone()
        if mine:
            return jsonify(mine), 200

    return error(409, "seat is not available")


@app.get("/reservations/<int:reservation_id>")
def get_reservation(reservation_id):
    user = current_user()
    if not user:
        return error(401, "send your user id in the X-User-Id header")
    with pool.connection() as conn:
        reservation = find_reservation(conn, reservation_id, user)
    if not reservation:
        return error(404, "reservation not found")
    return jsonify(reservation)


@app.get("/bookings")
def booking_history():
    user = current_user()
    if not user:
        return error(401, "send your user id in the X-User-Id header")
    with pool.connection() as conn:
        rows = conn.execute(f"SELECT {RESERVATION_COLUMNS} FROM reservations WHERE user_id = %s ORDER BY id DESC",
                            (user,)).fetchall()
    return jsonify(rows)


@app.post("/reservations/<int:reservation_id>/pay")
def pay(reservation_id):
    user = current_user()
    if not user:
        return error(401, "send your user id in the X-User-Id header")
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in ("success", "fail", "delayed"):
        return error(400, "mode must be success, fail or delayed")

    with pool.connection() as conn:
        reservation = find_reservation(conn, reservation_id, user)
    if not reservation:
        return error(404, "reservation not found")
    if reservation["status"] == "confirmed":
        return jsonify(reservation), 200  # already paid: paying again changes nothing
    if reservation["status"] != "held":
        return error(409, f"reservation is {reservation['status']}, it can't be paid")

    # Simulate the payment gateway. We hold no database connection or lock while waiting,
    # so a slow payment never blocks other students.
    if mode == "delayed":
        time.sleep(PAYMENT_DELAY_SECONDS)

    with pool.connection() as conn:
        if mode == "fail":
            conn.execute("INSERT INTO payments (reservation_id, mode, status) VALUES (%s, %s, 'failed')",
                         (reservation_id, mode))
            return error(402, "payment failed; your seat is still held, you can try again")

        # Book the seat only if this reservation still owns it and the hold has not run out.
        booked = conn.execute("""
            UPDATE seats SET status = 'booked', hold_expires_at = NULL
            WHERE id = %s AND reservation_id = %s AND status = 'held' AND hold_expires_at >= now()""",
            (reservation["seat_id"], reservation_id)).rowcount == 1

        if booked:
            conn.execute("UPDATE reservations SET status = 'confirmed' WHERE id = %s", (reservation_id,))
            conn.execute("INSERT INTO payments (reservation_id, mode, status) VALUES (%s, %s, 'succeeded')",
                         (reservation_id, mode))
            return jsonify(find_reservation(conn, reservation_id, user)), 200

        # Not booked. Either a duplicate pay request already booked it, or the hold ran out while paying.
        reservation = find_reservation(conn, reservation_id, user)
        if reservation["status"] == "confirmed":
            return jsonify(reservation), 200
        conn.execute("INSERT INTO payments (reservation_id, mode, status) VALUES (%s, %s, 'too_late')",
                     (reservation_id, mode))
        return error(409, "your hold expired before the payment finished; you were not charged")


@app.post("/reservations/<int:reservation_id>/cancel")
def cancel(reservation_id):
    user = current_user()
    if not user:
        return error(401, "send your user id in the X-User-Id header")

    with pool.connection() as conn:
        reservation = find_reservation(conn, reservation_id, user)
        if not reservation:
            return error(404, "reservation not found")
        if reservation["status"] in ("cancelled", "expired"):
            return jsonify(reservation), 200  # already over: cancelling again changes nothing

        # Free the seat, but only if this reservation still owns it.
        # This stops an old request from freeing a seat that now belongs to someone else.
        freed = conn.execute("""
            UPDATE seats SET status = 'available', reservation_id = NULL, hold_expires_at = NULL
            WHERE id = %s AND reservation_id = %s""", (reservation["seat_id"], reservation_id)).rowcount == 1

        conn.execute("UPDATE reservations SET status = %s WHERE id = %s",
                     ("cancelled" if freed else "expired", reservation_id))
        conn.execute("UPDATE payments SET status = 'refunded' WHERE reservation_id = %s AND status = 'succeeded'",
                     (reservation_id,))
        return jsonify(find_reservation(conn, reservation_id, user)), 200


# Pretty-print JSON responses (easy to read in a browser) and keep keys in the order we wrote them.
app.json.compact = False
app.json.sort_keys = False

if __name__ == "__main__":
    app.run(port=8000, threaded=True)
