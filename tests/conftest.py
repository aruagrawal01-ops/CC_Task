import os
import pathlib

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST_DB = os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost:5432/lastseat_test")
os.environ.setdefault("POOL_MAX", "64")

from app import main, service  # noqa: E402  (env must be set before the pool is created)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute((ROOT / "db" / "init.sql").read_text())
    monkeypatch.setattr(service, "HOLD_SECONDS", 120)
    monkeypatch.setattr(service, "PAYMENT_DELAY_SECONDS", 0.2)
    yield
    assert_invariants()


def sql(query, *params):
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        cur = c.execute(query, params)
        return cur.fetchall() if cur.description else cur.rowcount


def assert_invariants():
    text = (ROOT / "loadtest" / "invariants.sql").read_text()
    stmts = [s for s in text.split(";") if "SELECT" in s]
    for s in stmts:
        assert sql(s) == [], f"invariant violated:\n{s.strip()}"


def expire_hold(seat_id):
    sql("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE id = %s", seat_id)


@pytest.fixture
def client():
    return main.create_app().test_client()


class Api:
    def __init__(self, client, user):
        self.c, self.user = client, user

    def _h(self, key=None):
        h = {"X-User-Id": self.user}
        if key:
            h["Idempotency-Key"] = key
        return h

    def reserve(self, seat_id, key=None):
        return self.c.post("/reservations", json={"seat_id": seat_id}, headers=self._h(key))

    def pay(self, rid, mode="success", key=None):
        return self.c.post(f"/reservations/{rid}/pay", json={"mode": mode}, headers=self._h(key))

    def cancel(self, rid, key=None):
        return self.c.post(f"/reservations/{rid}/cancel", headers=self._h(key))

    def get(self, path):
        return self.c.get(path, headers=self._h())


@pytest.fixture
def alice(client):
    return Api(client, "alice")


@pytest.fixture
def bob(client):
    return Api(client, "bob")
