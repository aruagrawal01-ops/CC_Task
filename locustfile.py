"""Load test: many students open the seat map, try to grab a free seat, then pay.
Run: locust -f locustfile.py --headless -u 1000 -r 100 -t 60s -H http://localhost:8000"""
import random
import uuid

from locust import HttpUser, between, task


class Student(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        self.client.headers["X-User-Id"] = "student-" + uuid.uuid4().hex[:8]
        self.has_ticket = False

    @task
    def try_to_book(self):
        response = self.client.get("/seats")
        if self.has_ticket or response.status_code != 200:
            return
        free = [s["id"] for s in response.json()["seats"] if s["status"] == "available"]
        if not free:
            return

        with self.client.post("/reservations", json={"seat_id": random.choice(free)}, catch_response=True) as r:
            if r.status_code == 409:
                r.success()  # someone else got the seat first: that is correct behaviour, not an error
                return
        if r.status_code != 201:
            return

        mode = random.choice(["success", "success", "success", "fail", "delayed"])
        with self.client.post(f"/reservations/{r.json()['id']}/pay", json={"mode": mode},
                              name="/reservations/[id]/pay", catch_response=True) as p:
            if p.status_code == 402:
                p.success()  # simulated card decline: expected
            elif p.status_code == 200:
                self.has_ticket = True
