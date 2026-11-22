"""Mini-ERP demo for shared and dedicated tenant environments."""
import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg_pool import PoolTimeout
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import db, guardrail, simulation, whale

CUSTOMER_PASSWORD = os.getenv("ERP_CUSTOMER_PASSWORD", "")
OPS_PASSWORD = os.getenv("ERP_OPS_PASSWORD", "")
basic = HTTPBasic(auto_error=False)


def require_access(request: Request, credentials: HTTPBasicCredentials = Depends(basic)):
    if db.SCOPE == "demo" or request.url.path in {"/health", "/ready"}:
        return
    path = request.url.path
    operator_route = path == "/ops" or path.startswith("/admin/")
    if credentials:
        if credentials.username == "ops":
            expected = OPS_PASSWORD
        elif credentials.username == "customer" and not operator_route:
            expected = CUSTOMER_PASSWORD
        else:
            expected = ""
        if expected and secrets.compare_digest(credentials.password.encode(), expected.encode()):
            return
    raise HTTPException(
        status_code=401,
        detail="credentials required for this environment",
        headers={"WWW-Authenticate": f'Basic realm="ERP {db.SCOPE}"'},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pending_reports = 0
    app.state.report_lock = asyncio.Lock()
    app.state.next_report_at = 0.0
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Mini-ERP",
    dependencies=[Depends(require_access)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def measure(request: Request, call_next):
    tenant_id = guardrail.tenant_from_path(request.url.path)
    start = time.perf_counter()
    response = await call_next(request)
    if tenant_id is not None:
        guardrail.record(tenant_id, (time.perf_counter() - start) * 1000, response.status_code)
    return response


class Line(BaseModel):
    product: str
    qty: int = Field(gt=0)
    unit_price: Decimal = Field(gt=0)


class OrderIn(BaseModel):
    lines: list[Line] = Field(min_length=1)


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def page_client():
    return FileResponse(STATIC_DIR / "index.html")


def require_whale_environment():
    if not db.serves_tenant(guardrail.WHALE_ID):
        raise HTTPException(status_code=404, detail="whale operations unavailable")


@app.get("/ops", include_in_schema=False)
def page_ops(_: None = Depends(require_whale_environment)):
    return FileResponse(STATIC_DIR / "ops.html")


@app.get("/api/tenants")
def list_tenants(response: Response):
    """Tenant list for the UI dropdown in this environment."""
    response.headers["X-ERP-Scope"] = db.SCOPE
    with db.connection() as conn:
        return conn.execute(
            "SELECT id, name, plan FROM tenants WHERE id <= 20 ORDER BY id"
        ).fetchall()


@app.get("/health")
def health():
    """Process liveness."""
    return {"ok": True}


@app.get("/ready")
def readiness():
    """Check that the configured database and tenant table are reachable."""
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1 FROM tenants LIMIT 1")
    except (psycopg.Error, PoolTimeout):
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"ok": True}


@app.get("/t/{tenant_id}/orders")
def list_orders(tenant_id: int = Depends(guardrail.check), limit: int = Query(50, ge=1, le=500)):
    """Light, OLTP-style query: what small tenants do all day."""
    with db.tenant_conn(tenant_id) as conn:
        return conn.execute(
            "SELECT id, order_date, status, total FROM orders "
            "WHERE tenant_id = %s ORDER BY order_date DESC, id DESC LIMIT %s",
            (tenant_id, limit),
        ).fetchall()


@app.post("/t/{tenant_id}/orders", status_code=201)
def create_order(order: OrderIn, tenant_id: int = Depends(guardrail.check)):
    total = sum(l.qty * l.unit_price for l in order.lines)
    try:
        with db.tenant_conn(tenant_id) as conn:
            row = conn.execute(
                "INSERT INTO orders (tenant_id, order_date, status, total) "
                "VALUES (%s, CURRENT_DATE, 'new', %s) RETURNING id",
                (tenant_id, total),
            ).fetchone()
            conn.cursor().executemany(
                "INSERT INTO order_lines (tenant_id, order_id, product, qty, unit_price) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(tenant_id, row["id"], l.product, l.qty, l.unit_price) for l in order.lines],
            )
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(status_code=404, detail="unknown tenant")
    return {"id": row["id"], "total": total}


REPORT_INTERVAL = float(os.getenv("ERP_REPORT_INTERVAL_SECONDS", "0.15"))
REPORT_QUEUE_SIZE = int(os.getenv("ERP_REPORT_QUEUE_SIZE", "24"))
if not 0 < REPORT_INTERVAL <= 1 or not 1 <= REPORT_QUEUE_SIZE <= 40:
    raise ValueError("Invalid dedicated report interval or queue size")


@app.get("/t/{tenant_id}/reports/monthly")
async def monthly_report(
    request: Request, tenant_id: int = Depends(guardrail.check),
    year: int = Query(2026, ge=1, le=9998),
):
    if db.SCOPE != "dedicated":
        return await run_in_threadpool(query_monthly_report, tenant_id, year)

    state = request.app.state
    if state.pending_reports >= REPORT_QUEUE_SIZE:
        raise HTTPException(429, "month-end report queue is full", headers={"Retry-After": "1"})
    state.pending_reports += 1
    try:
        async with state.report_lock:
            await asyncio.sleep(max(0, state.next_report_at - time.monotonic()))
            state.next_report_at = time.monotonic() + REPORT_INTERVAL
            return await run_in_threadpool(query_monthly_report, tenant_id, year)
    finally:
        state.pending_reports -= 1


def query_monthly_report(tenant_id: int, year: int):
    with db.tenant_conn(tenant_id) as conn:
        return conn.execute(
            """
            SELECT date_trunc('month', o.order_date)::date AS month,
                   l.product,
                   sum(l.qty * l.unit_price) AS revenue,
                   sum(l.qty)                AS units
            FROM orders o
            JOIN order_lines l ON l.order_id = o.id AND l.tenant_id = o.tenant_id
            WHERE o.tenant_id = %s AND o.order_date >= %s AND o.order_date < %s
            GROUP BY 1, 2
            ORDER BY 1, revenue DESC
            LIMIT 200
            """,
            (tenant_id, date(year, 1, 1), date(year + 1, 1, 1)),
        ).fetchall()


@app.get("/admin/metrics")
def metrics():
    """Whale vs. small tenants latency + connection pool pressure."""
    return {**guardrail.snapshot(), "pools": db.pool_stats()}


@app.post("/admin/metrics/reset")
def metrics_reset():
    guardrail.reset()
    return {"reset": True}


@app.get("/admin/whale")
def whale_status(_: None = Depends(require_whale_environment)):
    return whale.status()


@app.post("/admin/whale/remove")
def whale_remove(_: None = Depends(require_whale_environment)):
    whale.remove()
    return whale.status()


@app.post("/admin/whale/add")
def whale_add(_: None = Depends(require_whale_environment)):
    whale.add()
    return whale.status()


@app.get("/admin/simulation")
def simulation_status(_: None = Depends(require_whale_environment)):
    return {"running": simulation.running()}


@app.post("/admin/simulation/start")
def simulation_start(_: None = Depends(require_whale_environment)):
    simulation.start()
    return {"running": simulation.running()}


@app.post("/admin/simulation/stop")
def simulation_stop(_: None = Depends(require_whale_environment)):
    simulation.stop()
    return {"running": False}
