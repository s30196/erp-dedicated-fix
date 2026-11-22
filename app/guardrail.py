from __future__ import annotations

import re
import threading
from collections import defaultdict, deque

from fastapi import HTTPException

from . import db

WHALE_ID = 1
_PATH = re.compile(r"^/t/(\d+)/")

_lat: dict[int, deque] = defaultdict(lambda: deque(maxlen=1000))
_lock = threading.Lock()


def tenant_from_path(path: str):
    m = _PATH.match(path)
    return int(m.group(1)) if m else None


def record(tenant_id: int, ms: float, status: int) -> None:
    with _lock:
        _lat[tenant_id].append((ms, status))


def check(tenant_id: int) -> int:
    if not db.serves_tenant(tenant_id):
        raise HTTPException(status_code=404, detail="unknown tenant")
    return tenant_id


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(p / 100 * len(s)))], 1)


def _summ(samples: list[tuple[float, int]]) -> dict:
    ms = [m for m, _ in samples]
    return {
        "requests": len(ms),
        "errors": sum(1 for _, st in samples if st >= 400),
        "p50_ms": _pct(ms, 50),
        "p95_ms": _pct(ms, 95),
        "p99_ms": _pct(ms, 99),
    }


def snapshot() -> dict:
    """Latency summary: whale vs. all small tenants (last ~1000 requests per tenant)."""
    with _lock:
        whale = list(_lat.get(WHALE_ID, []))
        small = [s for tid, d in _lat.items() if tid != WHALE_ID for s in d]
        per_tenant = {tid: list(d) for tid, d in _lat.items() if tid != WHALE_ID}
    worst = sorted(
        ({"tenant_id": tid, **_summ(s)} for tid, s in per_tenant.items() if s),
        key=lambda x: x["p95_ms"] or 0,
        reverse=True,
    )[:5]
    return {"whale": _summ(whale), "small_tenants": _summ(small), "worst_small_tenants": worst}


def reset() -> None:
    with _lock:
        _lat.clear()
