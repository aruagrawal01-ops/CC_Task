import hashlib
import logging
import re
from functools import wraps

from flask import Flask, jsonify, request
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from werkzeug.exceptions import HTTPException

from . import db, service
from .service import ApiError

log = logging.getLogger("last-seat")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def current_user():
    # shortcut: X-User-Id stands in for auth; swap for a verified JWT `sub` before any real deployment.
    u = request.headers.get("X-User-Id", "")
    if not USER_RE.match(u):
        raise ApiError(401, "missing or invalid X-User-Id header")
    return u


def body_field(name, kind):
    data = request.get_json(silent=True)
    v = data.get(name) if isinstance(data, dict) else None
    if not isinstance(v, kind) or isinstance(v, bool):
        raise ApiError(422, f"body must be JSON with '{name}' ({kind.__name__})")
    return v


def idempotent(fn):
    """Optional Idempotency-Key header: the first response is stored and replayed for retries with the same key."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        key = request.headers.get("Idempotency-Key")
        if key is None:
            status, body = _call(fn, user, *args, **kwargs)
            return jsonify(body), status
        if not 1 <= len(key) <= 128:
            raise ApiError(422, "Idempotency-Key must be 1-128 characters")
        req_hash = hashlib.sha256(f"{request.method} {request.path}\n".encode() + request.get_data()).hexdigest()

        with db.conn() as c:
            claimed = c.execute("INSERT INTO idempotency_keys (user_id, key, request_hash) VALUES (%s, %s, %s) "
                                "ON CONFLICT DO NOTHING", (user, key, req_hash)).rowcount == 1
            prior = None if claimed else c.execute(
                "SELECT * FROM idempotency_keys WHERE user_id = %s AND key = %s", (user, key)).fetchone()
        if prior:
            if prior["request_hash"] != req_hash:
                raise ApiError(422, "Idempotency-Key was already used with a different request")
            if prior["status_code"] is None:
                raise ApiError(409, "a request with this Idempotency-Key is still in progress")
            return jsonify(prior["response"]), prior["status_code"], {"Idempotent-Replayed": "true"}

        try:
            status, body = _call(fn, user, *args, **kwargs)
        except Exception:
            # Unexpected failure (e.g. 503): release the key so the client can retry.
            with db.conn() as c:
                c.execute("DELETE FROM idempotency_keys WHERE user_id = %s AND key = %s", (user, key))
            raise
        # shortcut: a crash between the business commit and this write leaves the key "in progress" forever;
        # fix with a TTL on NULL rows or by writing the response in the same transaction as the business change.
        with db.conn() as c:
            c.execute("UPDATE idempotency_keys SET status_code = %s, response = %s WHERE user_id = %s AND key = %s",
                      (status, Jsonb(body), user, key))
        return jsonify(body), status
    return wrapper


def _call(fn, user, *args, **kwargs):
    """Business errors (402/409/410/...) are real responses and get stored for replay too."""
    try:
        return fn(user, *args, **kwargs)
    except ApiError as e:
        return e.status, {"error": e.message}


def create_app():
    app = Flask(__name__)
    logging.basicConfig(level=logging.INFO)

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(PoolTimeout)
    def busy(e):
        return jsonify(error="server busy, retry shortly"), 503, {"Retry-After": "1"}

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error=e.description), e.code

    @app.get("/health")
    def health():
        try:
            with db.conn() as c:
                c.execute("SELECT 1")
        except Exception:
            log.exception("health check failed")
            return jsonify(status="db unavailable"), 503
        return jsonify(status="ok")

    @app.get("/seats")
    def seats():
        rows = service.list_seats()
        return jsonify(available=sum(r["status"] == "available" for r in rows), seats=rows)

    @app.get("/seats/<int:seat_id>")
    def seat(seat_id):
        return jsonify(service.get_seat(seat_id))

    @app.post("/reservations")
    @idempotent
    def reserve(user):
        return service.reserve(user, body_field("seat_id", int))

    @app.get("/reservations/<int:rid>")
    def reservation(rid):
        return jsonify(service.get_reservation(current_user(), rid))

    @app.post("/reservations/<int:rid>/pay")
    @idempotent
    def pay(user, rid):
        return service.pay(user, rid, body_field("mode", str))

    @app.post("/reservations/<int:rid>/cancel")
    @idempotent
    def cancel(user, rid):
        return service.cancel(user, rid)

    @app.get("/users/me/bookings")
    def bookings():
        return jsonify(service.my_bookings(current_user()))

    return app
