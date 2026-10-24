"""Tests run against a real Postgres database. WARNING: they wipe the database given in TEST_DATABASE_URL."""
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/lastseat_test")

import psycopg  # noqa: E402
import pytest  # noqa: E402

import app as server  # noqa: E402

SCHEMA = (pathlib.Path(__file__).parent / "schema.sql").read_text()


@pytest.fixture(autouse=True)
def fresh_database(monkeypatch):
    sql(SCHEMA)
    monkeypatch.setattr(server, "PAYMENT_DELAY_SECONDS", 0.2)


def sql(query, *params):
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def call(method, path, user="alice", body=None):
    headers = {"X-User-Id": user} if user else {}
    response = server.app.test_client().open(path, method=method, json=body, headers=headers)
    return response.status_code, response.get_json()


def reserve(seat_id, user="alice"):
    return call("POST", "/reservations", user, {"seat_id": seat_id})


def pay(reservation_id, mode="success", user="alice"):
    return call("POST", f"/reservations/{reservation_id}/pay", user, {"mode": mode})


def cancel(reservation_id, user="alice"):
    return call("POST", f"/reservations/{reservation_id}/cancel", user)


def end_hold_now(seat_id):
    # Pretend the hold time has passed, instead of waiting 2 minutes.
    sql("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", seat_id)
    sql("UPDATE reservations SET expires_at = now() - interval '1 second' WHERE seat_id = %s AND status = 'held'", seat_id)


def seat_status(seat_id):
    return sql("SELECT status FROM seats WHERE id = %s", seat_id)[0][0]


def test_reserve_then_pay():
    code, r = reserve(7)
    assert code == 201 and r["status"] == "held"
    code, r = pay(r["id"])
    assert code == 200 and r["status"] == "confirmed"
    assert seat_status(7) == "booked"
    code, history = call("GET", "/bookings")
    assert [h["status"] for h in history] == ["confirmed"]


def test_seat_cannot_be_taken_twice():
    code, first = reserve(1)
    assert code == 201
    code, again = reserve(1)  # same user clicks twice: gets the same reservation back
    assert code == 200 and again["id"] == first["id"]
    code, _ = reserve(1, user="bob")
    assert code == 409


def test_failed_payment_keeps_the_hold():
    _, r = reserve(2)
    assert pay(r["id"], "fail")[0] == 402
    assert reserve(2, user="bob")[0] == 409  # still Alice's
    assert pay(r["id"], "success")[0] == 200
    assert sql("SELECT status FROM payments ORDER BY id") == [("failed",), ("succeeded",)]


def test_hold_expires_and_seat_goes_to_next_person():
    _, r = reserve(3)
    end_hold_now(3)
    assert reserve(3, user="bob")[0] == 201
    assert call("GET", f"/reservations/{r['id']}")[1]["status"] == "expired"
    assert pay(r["id"])[0] == 409


def test_delayed_payment_that_finishes_after_the_hold_expired(monkeypatch):
    monkeypatch.setattr(server, "HOLD_SECONDS", 1)
    monkeypatch.setattr(server, "PAYMENT_DELAY_SECONDS", 1.5)
    _, r = reserve(5)
    assert pay(r["id"], "delayed")[0] == 409
    assert seat_status(5) == "held"  # not booked; the expired hold is free for the next person
    assert sql("SELECT status FROM payments") == [("too_late",)]
    assert reserve(5, user="bob")[0] == 201


def test_paying_twice_at_the_same_time_charges_once():
    _, r = reserve(6)
    with ThreadPoolExecutor(2) as ex:
        codes = list(ex.map(lambda _: pay(r["id"], "delayed")[0], range(2)))
    assert codes == [200, 200]
    assert sql("SELECT status FROM payments") == [("succeeded",)]


def test_cancel_frees_the_seat():
    _, r = reserve(9)
    assert cancel(r["id"])[1]["status"] == "cancelled"
    assert cancel(r["id"])[0] == 200  # cancelling again is fine
    assert reserve(9, user="bob")[0] == 201


def test_cancel_after_paying_refunds():
    _, r = reserve(10)
    pay(r["id"])
    assert cancel(r["id"])[1]["status"] == "cancelled"
    assert sql("SELECT status FROM payments") == [("refunded",)]
    assert seat_status(10) == "available"


def test_old_cancel_cannot_free_someone_elses_seat():
    _, alices = reserve(11)
    end_hold_now(11)
    _, bobs = reserve(11, user="bob")
    code, r = cancel(alices["id"])
    assert code == 200 and r["status"] == "expired"
    assert sql("SELECT status, reservation_id FROM seats WHERE id = 11") == [("held", bobs["id"])]


def test_bad_requests():
    assert call("POST", "/reservations", user=None, body={"seat_id": 1})[0] == 401
    assert call("POST", "/reservations", body={"seat_id": "1"})[0] == 400
    assert reserve(9999)[0] == 404
    _, r = reserve(1)
    assert pay(r["id"], "maybe")[0] == 400
    assert call("GET", f"/reservations/{r['id']}", user="bob")[0] == 404  # not Bob's reservation
    assert call("GET", "/seats")[1]["available"] == 499


def test_200_people_one_seat_only_one_wins():
    with ThreadPoolExecutor(50) as ex:
        codes = list(ex.map(lambda i: reserve(42, user=f"user{i}")[0], range(200)))
    assert codes.count(201) == 1
    assert codes.count(409) == 199


def test_10000_people_500_seats_no_double_booking():
    with ThreadPoolExecutor(50) as ex:
        codes = list(ex.map(lambda i: reserve(i % 500 + 1, user=f"user{i}")[0], range(10_000)))
    assert codes.count(201) == 500
    # every seat is held exactly once, by exactly one live reservation
    assert sql("SELECT count(*) FROM seats WHERE status = 'held'") == [(500,)]
    assert sql("SELECT seat_id FROM reservations WHERE status = 'held' GROUP BY seat_id HAVING count(*) > 1") == []
