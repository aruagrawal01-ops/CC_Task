import os
import threading

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool = None
_lock = threading.Lock()


def pool():
    # One pool per process (per Gunicorn worker). Budget: replicas x workers x POOL_MAX < max_connections.
    global _pool
    if _pool is None:
        with _lock:  # gthread workers: without this, concurrent first requests each build their own pool
            if _pool is None:
                _pool = ConnectionPool(
                    os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/lastseat"),
                    min_size=1,
                    max_size=int(os.environ.get("POOL_MAX", "10")),
                    timeout=float(os.environ.get("POOL_TIMEOUT", "5")),
                    kwargs={"row_factory": dict_row},
                    open=True,
                )
    return _pool


def conn():
    """`with conn() as c:` borrows a connection; commits on success, rolls back on exception."""
    return pool().connection()
