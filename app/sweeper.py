"""Eager expiry loop. Correctness doesn't depend on it (reserve treats expired holds as free); it just keeps
the seat map and the reservations table honest without waiting for someone to try the seat."""
import logging
import os
import time

from .service import sweep

INTERVAL = float(os.environ.get("SWEEP_INTERVAL_SECONDS", "5"))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("sweeper")
    while True:
        try:
            n = sweep()
            if n:
                log.info("expired %d holds", n)
        except Exception:
            log.exception("sweep failed; retrying")
        time.sleep(INTERVAL)
