"""Compare small-tenant latency with and without distributor traffic."""
import argparse
import asyncio
import json
import math
import os
import random
import time

import httpx


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[math.ceil(p / 100 * len(s)) - 1]


async def order_worker(client, stop_at, samples, tenant_ids):
    while time.perf_counter() < stop_at:
        tid = random.choice(tenant_ids)
        t0 = time.perf_counter()
        try:
            if random.random() < 0.8:
                r = await client.get(f"/t/{tid}/orders", params={"limit": 20})
            else:
                r = await client.post(
                    f"/t/{tid}/orders",
                    json={"lines": [{"product": "SKU-1", "qty": 2, "unit_price": "9.99"}]},
                )
            ok = r.is_success
        except httpx.HTTPError:
            ok = False
        samples.append(((time.perf_counter() - t0) * 1000, ok))
        await asyncio.sleep(random.uniform(0.01, 0.05))  # think time


async def report_worker(client, stop_at, samples):
    while time.perf_counter() < stop_at:
        t0 = time.perf_counter()
        try:
            r = await client.get("/t/1/reports/monthly", params={"year": 2026})
            ok = r.is_success
        except httpx.HTTPError:
            ok = False
        samples.append(((time.perf_counter() - t0) * 1000, ok))


def summarize(samples, duration):
    latencies = [ms for ms, _ in samples]
    return {
        "requests": len(samples),
        "rps": round(len(samples) / duration, 1),
        "errors": sum(not ok for _, ok in samples),
        "p50_ms": pct(latencies, 50),
        "p95_ms": pct(latencies, 95),
        "p97_ms": pct(latencies, 97),
        "p99_ms": pct(latencies, 99),
        "within_800ms": sum(ok and ms <= 800 for ms, ok in samples),
    }


def customer_auth(variable):
    password = os.getenv(variable)
    return ("customer", password) if password else None


async def run_phase(args, with_whale):
    small, normal, reports = [], [], []
    shared_client = httpx.AsyncClient(base_url=args.base_url, timeout=5, auth=customer_auth("ERP_SHARED_PASSWORD"))
    whale_client = httpx.AsyncClient(
        base_url=args.whale_url or args.base_url, timeout=5,
        auth=customer_auth("ERP_WHALE_PASSWORD" if args.whale_url else "ERP_SHARED_PASSWORD"),
    )
    async with shared_client as shared, whale_client as whale:
        stop_at = time.perf_counter() + args.duration
        small_ids = range(2, args.small_tenants + 2)
        tasks = [order_worker(shared, stop_at, small, small_ids)
                 for _ in range(args.small_workers)]
        if with_whale:
            tasks += [order_worker(whale, stop_at, normal, (1,))
                      for _ in range(args.whale_order_workers)]
            tasks += [report_worker(whale, stop_at, reports)
                      for _ in range(args.whale_workers)]
        await asyncio.gather(*tasks)
    return {
        "small": summarize(small, args.duration),
        "whale_normal": summarize(normal, args.duration),
        "whale_reports": summarize(reports, args.duration),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--whale-url", help="dedicated API URL, defaults to the shared URL")
    ap.add_argument("--whale-order-workers", type=int, default=10)
    ap.add_argument("--duration", type=int, default=20, help="seconds per phase")
    ap.add_argument("--small-workers", type=int, default=10)
    ap.add_argument("--whale-workers", type=int, default=8, help="concurrent whale report runners")
    ap.add_argument("--small-tenants", type=int, default=300)
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    for field in ("duration", "small_workers", "whale_workers", "whale_order_workers", "small_tenants"):
        if getattr(args, field) < 1:
            ap.error(f"--{field.replace('_', '-')} must be positive")

    print(f"[{args.label}] phase 1: small tenants only ({args.duration}s)")
    base = await run_phase(args, False)
    print(f"[{args.label}] phase 2: + whale, {args.whale_workers} report workers ({args.duration}s)")
    with_whale = await run_phase(args, True)

    baseline = base["small"]
    loaded = with_whale["small"]
    normal = with_whale["whale_normal"]
    degradation = (loaded["p95_ms"] / baseline["p95_ms"] - 1) * 100 if baseline["p95_ms"] else None
    normal_fraction = normal["within_800ms"] / normal["requests"] if normal["requests"] else 0
    checks = {
        "whale_normal_at_least_97_percent_within_800ms": normal_fraction >= 0.97,
        "small_p95_increase_at_most_10_percent": (
            degradation is not None and degradation <= 10
            and loaded["errors"] == baseline["errors"] == 0
        ),
        "whale_reports_successful": (
            with_whale["whale_reports"]["requests"] > 0 and with_whale["whale_reports"]["errors"] == 0
        ),
    }
    result = {
        "label": args.label, "args": vars(args), "small_only": base, "with_whale": with_whale,
        "small_p95_change_percent": degradation, "whale_normal_fraction_within_800ms": normal_fraction,
        "checks": checks,
    }
    os.makedirs("results", exist_ok=True)
    with open(f"results/{args.label}.json", "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
