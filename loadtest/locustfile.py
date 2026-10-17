"""Opening-night rush: each simulated student looks at the map, grabs a free seat, then pays (or fails/abandons).
4xx outcomes like 409 (seat taken) are correct behaviour under contention, so they count as successes."""
import random
import uuid

from locust import HttpUser, between, task

EXPECTED = {200, 201, 402, 409, 410}


class Student(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.client.headers["X-User-Id"] = f"s-{uuid.uuid4().hex[:12]}"
        self.rid = None

    def call(self, method, path, name, **kw):
        with self.client.request(method, path, name=name, catch_response=True, **kw) as r:
            if r.status_code in EXPECTED:
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")
            return r

    @task
    def book(self):
        if self.rid:
            return  # already has a seat
        seats = self.call("GET", "/seats", "/seats").json().get("seats", [])
        free = [s["id"] for s in seats if s["status"] == "available"] or [random.randint(1, 500)]
        r = self.call("POST", "/reservations", "/reservations", json={"seat_id": random.choice(free)},
                      headers={"Idempotency-Key": uuid.uuid4().hex})
        if r.status_code != 201:
            return
        rid = r.json()["id"]
        roll = random.random()
        if roll < 0.10:
            return  # abandons; hold expires
        mode = "fail" if roll < 0.25 else "delayed" if roll < 0.35 else "success"
        p = self.call("POST", f"/reservations/{rid}/pay", "/reservations/[id]/pay", json={"mode": mode})
        if p.status_code == 402:
            p = self.call("POST", f"/reservations/{rid}/pay", "/reservations/[id]/pay", json={"mode": "success"})
        if p.status_code == 200:
            self.rid = rid
            if random.random() < 0.05:
                self.call("POST", f"/reservations/{rid}/cancel", "/reservations/[id]/cancel")
                self.rid = None
