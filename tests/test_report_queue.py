import asyncio
import unittest
from unittest.mock import patch

import httpx

from app import db, main


class ReportQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_queue_rejects_then_recovers(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def query(*args):
            started.set()
            await release.wait()
            return [{"product": "SKU-1"}]

        with (
            patch.object(db, "SCOPE", "dedicated"),
            patch.object(main, "CUSTOMER_PASSWORD", "test-password"),
            patch.object(main, "REPORT_QUEUE_SIZE", 1),
            patch.object(main, "REPORT_INTERVAL", 0.01),
            patch.object(main, "run_in_threadpool", side_effect=query),
        ):
            async with main.lifespan(main.app):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test",
                    auth=("customer", "test-password"),
                ) as client:
                    url = "/t/1/reports/monthly"
                    request = asyncio.create_task(client.get(url))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=2)
                        response = await client.get(url)
                        self.assertEqual(response.status_code, 429)
                        self.assertEqual(response.headers["Retry-After"], "1")
                    finally:
                        release.set()
                        first = await request
                    self.assertEqual(first.json(), [{"product": "SKU-1"}])
                    self.assertEqual((await client.get(url)).status_code, 200)

    async def test_concurrent_queries_start_at_configured_interval(self):
        timestamps = []

        async def query(*args):
            timestamps.append(main.time.monotonic())
            return []

        interval = 0.05
        with (
            patch.object(db, "SCOPE", "dedicated"),
            patch.object(main, "CUSTOMER_PASSWORD", "test-password"),
            patch.object(main, "REPORT_QUEUE_SIZE", 2),
            patch.object(main, "REPORT_INTERVAL", interval),
            patch.object(main, "run_in_threadpool", side_effect=query),
        ):
            async with main.lifespan(main.app):
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test",
                    auth=("customer", "test-password"),
                ) as client:
                    url = "/t/1/reports/monthly"
                    responses = await asyncio.gather(client.get(url), client.get(url))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertGreaterEqual(timestamps[1] - timestamps[0], interval - 0.005)
