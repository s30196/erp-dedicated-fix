"""Database access for a single ERP environment."""
import os
import threading
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_DSN = os.getenv("DATABASE_URL", "postgresql://erp:erp@localhost:5432/erp")
POOL_MAX = int(os.getenv("POOL_MAX", "10"))
POOL_TIMEOUT = float(os.getenv("POOL_TIMEOUT", "10"))  # seconds to wait for a free connection

SCOPE = os.getenv("ERP_SCOPE", "demo")
if SCOPE not in {"demo", "shared", "dedicated"}:
    raise ValueError("ERP_SCOPE must be demo, shared or dedicated")


def serves_tenant(tenant_id: int) -> bool:
    if SCOPE == "shared":
        return tenant_id > 1
    if SCOPE == "dedicated":
        return tenant_id == 1
    return tenant_id >= 1


_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def _pool_for(dsn: str) -> ConnectionPool:
    with _lock:
        if dsn not in _pools:
            _pools[dsn] = ConnectionPool(
                dsn,
                min_size=1,
                max_size=POOL_MAX,
                timeout=POOL_TIMEOUT,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return _pools[dsn]


@contextmanager
def connection():
    with _pool_for(DEFAULT_DSN).connection() as conn:
        yield conn


@contextmanager
def tenant_conn(tenant_id: int):
    if not serves_tenant(tenant_id):
        raise ValueError("tenant is outside this ERP environment")
    with connection() as conn:
        yield conn


def pool_stats() -> dict:
    """Pool pressure per database - 'requests_waiting' > 0 means queueing."""
    out = {}
    with _lock:
        pools = list(_pools.values())
    for pool in pools:
        s = pool.get_stats()
        label = SCOPE
        out[label] = {
            "pool_size": s.get("pool_size"),
            "pool_available": s.get("pool_available"),
            "requests_waiting": s.get("requests_waiting"),
            "requests_errors": s.get("requests_errors"),
        }
    return out
