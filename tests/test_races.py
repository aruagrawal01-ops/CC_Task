"""Real concurrency against real Postgres. Threads call the service layer directly (the HTTP layer adds nothing
to the race), each on its own pooled connection."""
import random
from concurrent.futures import ThreadPoolExecutor

from app import service
from app.service import ApiError
from conftest import sql


def attempt(fn, *args):
    try:
        return fn(*args)[0]
    except ApiError as e:
        return e.status


def test_200_threads_one_seat_one_winner():
    with ThreadPoolExecutor(64) as ex:
        codes = list(ex.map(lambda i: attempt(service.reserve, f"u{i}", 42), range(200)))
    assert codes.count(201) == 1 and codes.count(409) == 199


def test_10k_users_500_seats():
    with ThreadPoolExecutor(64) as ex:
        codes = list(ex.map(lambda i: attempt(service.reserve, f"u{i}", i % 500 + 1), range(10_000)))
    assert codes.count(201) == 500 and codes.count(409) == 9_500
    holds = sql("SELECT id, user_id FROM reservations WHERE status = 'held'")
    with ThreadPoolExecutor(64) as ex:
        paid = list(ex.map(lambda h: attempt(service.pay, h[1], h[0], "success"), holds))
    assert paid.count(200) == 500
    assert sql("SELECT count(*) FROM seats WHERE status = 'booked'")[0][0] == 500


def test_chaos():
    """Random reserve/pay/cancel/expire/sweep on a few hot seats; the invariants checked after each test decide."""
    random.seed(7)

    def user_session(i):
        u = f"c{i % 40}"
        for _ in range(15):
            op = random.random()
            seat = random.randint(1, 10)
            try:
                if op < 0.5:
                    service.reserve(u, seat)
                else:
                    mine = service.my_bookings(u)
                    if not mine:
                        continue
                    rid = random.choice(mine)["id"]
                    if op < 0.75:
                        service.pay(u, rid, random.choice(["success", "fail", "delayed"]))
                    elif op < 0.9:
                        service.cancel(u, rid)
                    elif op < 0.95:
                        sql("UPDATE seats SET hold_expires_at = now() - interval '1 second' "
                            "WHERE id = %s AND status = 'held'", seat)
                    else:
                        service.sweep()
            except ApiError:
                pass

    service.PAYMENT_DELAY_SECONDS = 0.01
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(user_session, range(120)))
