import threading

from app import service
from conftest import expire_hold, sql


def test_happy_path(alice):
    r = alice.reserve(7)
    assert r.status_code == 201 and r.json["status"] == "held"
    rid = r.json["id"]
    p = alice.pay(rid)
    assert p.status_code == 200 and p.json["status"] == "confirmed"
    assert alice.get("/seats/7").json["status"] == "booked"
    assert [b["status"] for b in alice.get("/users/me/bookings").json] == ["confirmed"]
    assert alice.pay(rid).status_code == 200  # paying again is a no-op, not a second charge
    assert sql("SELECT count(*) FROM payments")[0][0] == 1


def test_conflict_vs_natural_idempotency(alice, bob):
    rid = alice.reserve(1).json["id"]
    again = alice.reserve(1)
    assert again.status_code == 200 and again.json["id"] == rid
    assert bob.reserve(1).status_code == 409


def test_failed_payment_keeps_hold_then_retry(alice, bob):
    rid = alice.reserve(2).json["id"]
    assert alice.pay(rid, "fail").status_code == 402
    assert bob.reserve(2).status_code == 409  # still alice's
    assert alice.pay(rid).status_code == 200
    assert sql("SELECT status FROM payments ORDER BY id") == [("failed",), ("succeeded",)]


def test_lazy_expiry_lets_next_user_in(alice, bob):
    rid = alice.reserve(3).json["id"]
    expire_hold(3)
    assert alice.get("/seats/3").json["status"] == "available"
    r = bob.reserve(3)
    assert r.status_code == 201
    assert alice.get(f"/reservations/{rid}").json["status"] == "expired"
    assert alice.pay(rid).status_code == 410


def test_sweeper_frees_expired_holds(alice):
    alice.reserve(4)
    assert service.sweep() == 0
    expire_hold(4)
    assert service.sweep() == 1
    assert sql("SELECT status, reservation_id FROM seats WHERE id = 4") == [("available", None)]


def test_delayed_payment_after_expiry_is_voided(alice, bob, monkeypatch):
    monkeypatch.setattr(service, "HOLD_SECONDS", 1)
    monkeypatch.setattr(service, "PAYMENT_DELAY_SECONDS", 1.5)
    rid = alice.reserve(5).json["id"]
    r = alice.pay(rid, "delayed")
    assert r.status_code == 409
    assert sql("SELECT status FROM payments") == [("void",)]
    assert bob.reserve(5).status_code == 201


def test_delayed_payment_in_time_succeeds(alice):
    rid = alice.reserve(6).json["id"]
    assert alice.pay(rid, "delayed").status_code == 200


def test_concurrent_double_pay_charges_once(alice, client, monkeypatch):
    monkeypatch.setattr(service, "PAYMENT_DELAY_SECONDS", 0.5)
    rid = alice.reserve(8).json["id"]
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(alice.pay(rid, "delayed").status_code)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [200, 409]
    assert sql("SELECT status FROM payments") == [("succeeded",)]


def test_cancel_is_repeatable_and_reallocates(alice, bob):
    rid = alice.reserve(9).json["id"]
    assert alice.cancel(rid).json["status"] == "cancelled"
    assert alice.cancel(rid).status_code == 200
    assert bob.reserve(9).status_code == 201


def test_cancel_confirmed_refunds(alice, bob):
    rid = alice.reserve(10).json["id"]
    alice.pay(rid)
    assert alice.cancel(rid).json["status"] == "cancelled"
    assert sql("SELECT status FROM payments") == [("refunded",)]
    assert bob.reserve(10).status_code == 201


def test_stale_cancel_cannot_free_someone_elses_seat(alice, bob):
    old = alice.reserve(11).json["id"]
    expire_hold(11)
    new = bob.reserve(11).json["id"]
    r = alice.cancel(old)
    assert r.status_code == 200 and r.json["status"] == "expired"
    assert sql("SELECT status, reservation_id FROM seats WHERE id = 11") == [("held", new)]


def test_one_live_seat_per_user(alice):
    alice.reserve(12)
    assert alice.reserve(13).status_code == 409
    expire_hold(12)  # expired but not yet swept: reserve should clean it up itself
    assert alice.reserve(13).status_code == 201


def test_idempotency_key_replay_and_mismatch(alice):
    a = alice.reserve(14, key="k1")
    b = alice.reserve(14, key="k1")
    assert a.status_code == b.status_code == 201  # replayed, not the natural-idempotency 200
    assert a.json == b.json and b.headers["Idempotent-Replayed"] == "true"
    assert alice.reserve(15, key="k1").status_code == 422
    rid = a.json["id"]
    assert alice.pay(rid, "fail", key="p1").status_code == 402
    assert alice.pay(rid, "fail", key="p1").status_code == 402  # same key -> same answer, no second attempt
    assert alice.pay(rid, "success", key="p2").status_code == 200
    assert sql("SELECT count(*) FROM payments")[0][0] == 2


def test_idempotency_key_in_progress(alice):
    sql("INSERT INTO idempotency_keys (user_id, key, request_hash) VALUES ('alice', 'busy', 'x')")
    assert alice.reserve(16, key="busy").status_code in (409, 422)


def test_validation(client, alice, bob):
    assert client.post("/reservations", json={"seat_id": 1}).status_code == 401
    assert alice.c.post("/reservations", json={"seat_id": "1"}, headers={"X-User-Id": "alice"}).status_code == 422
    assert alice.c.post("/reservations", data="nope", headers={"X-User-Id": "alice"}).status_code == 422
    assert alice.reserve(9999).status_code == 404
    rid = alice.reserve(1).json["id"]
    assert alice.pay(rid, "maybe").status_code == 422
    assert bob.get(f"/reservations/{rid}").status_code == 404
    assert bob.pay(rid).status_code == 404
    assert bob.cancel(rid).status_code == 404
    assert client.get("/health").json == {"status": "ok"}
    s = client.get("/seats").json
    assert len(s["seats"]) == 500 and s["available"] == 499
