"""Circuit state and real asyncio execution tests; no model/database calls.

Run with: python -m unittest discover -s tests -p test_circuit_breaker.py -v
"""
import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from mcp.tool_manager import CircuitBreaker, CircuitState, MCPToolManager, Tool


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("mcp.tool_manager.time.monotonic", return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.breaker = CircuitBreaker(failure_threshold=2, recovery_s=10)

    def open_breaker(self):
        self.breaker.record_failure(self.breaker.acquire())
        self.breaker.record_failure(self.breaker.acquire())
        self.assertEqual(self.breaker.state, CircuitState.OPEN)

    def probe(self):
        self.open_breaker()
        self.now.return_value = 110.0
        permit = self.breaker.acquire()
        self.assertIsNotNone(permit)
        self.assertTrue(permit.probe)
        return permit

    def test_closed_success_resets_consecutive_failures(self):
        self.breaker.record_failure(self.breaker.acquire())
        self.breaker.record_success(self.breaker.acquire())
        self.assertEqual(self.breaker.fail_count, 0)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

    def test_open_blocks_requests_until_recovery_boundary(self):
        self.open_breaker()
        opened_at = self.breaker.opened_at
        self.now.return_value = 109.999
        self.assertIsNone(self.breaker.acquire())
        self.assertEqual(self.breaker.opened_at, opened_at)
        self.now.return_value = 110.0
        self.assertIsNotNone(self.breaker.acquire())

    def test_half_open_admits_only_one_probe(self):
        self.probe()
        self.assertTrue(all(self.breaker.acquire() is None for _ in range(20)))

    def test_boolean_compatibility_method_also_blocks_second_probe(self):
        self.open_breaker()
        self.now.return_value = 110.0
        self.assertTrue(self.breaker.allow())
        self.assertFalse(self.breaker.allow())
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)

    def test_threads_compete_for_one_probe(self):
        self.open_breaker()
        self.now.return_value = 110.0
        with ThreadPoolExecutor(max_workers=8) as pool:
            permits = list(pool.map(lambda _: self.breaker.acquire(), range(40)))
        self.assertEqual(sum(permit is not None for permit in permits), 1)

    def test_probe_success_closes_and_resets_breaker(self):
        permit = self.probe()
        self.breaker.record_success(permit)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.fail_count, 0)
        self.assertIsNone(self.breaker.opened_at)
        self.assertIsNotNone(self.breaker.acquire())

    def test_probe_failure_starts_new_cooldown(self):
        permit = self.probe()
        self.now.return_value = 115.0
        self.breaker.record_failure(permit)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.opened_at, 115.0)
        self.now.return_value = 124.999
        self.assertIsNone(self.breaker.acquire())
        self.now.return_value = 125.0
        self.assertIsNotNone(self.breaker.acquire())

    def test_cancelled_probe_starts_new_cooldown_without_failure_count(self):
        permit = self.probe()
        failures = self.breaker.fail_count
        self.now.return_value = 115.0
        self.breaker.record_cancelled(permit)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.assertEqual(self.breaker.fail_count, failures)
        self.assertIsNone(self.breaker.acquire())
        self.now.return_value = 125.0
        self.assertIsNotNone(self.breaker.acquire())

    def test_old_success_does_not_close_open_or_half_open_breaker(self):
        old = self.breaker.acquire()
        self.open_breaker()
        self.breaker.record_success(old)
        self.assertEqual(self.breaker.state, CircuitState.OPEN)
        self.now.return_value = 110.0
        self.breaker.acquire()
        self.breaker.record_success(old)
        self.assertEqual(self.breaker.state, CircuitState.HALF_OPEN)

    def test_old_failure_and_cleanup_cannot_undo_successful_probe(self):
        old = self.breaker.acquire()
        probe = self.probe()
        self.breaker.record_success(probe)
        self.breaker.record_failure(old)
        self.breaker.record_failure(probe)
        self.breaker.record_cancelled(probe)
        self.assertEqual(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.fail_count, 0)


class ToolCircuitExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Exercise real manager code without creating an SDK client.
        self.manager = object.__new__(MCPToolManager)
        self.manager._tools = {}
        self.manager._cache = {}

    def register(self, handler, *, timeout_s=5, fallback=None):
        tool = Tool("test_tool", "offline test", handler, {}, timeout_s=timeout_s, fallback=fallback)
        tool.breaker = CircuitBreaker(failure_threshold=1, recovery_s=0)
        self.manager.register(tool)
        return tool

    async def test_many_requests_do_not_execute_while_probe_is_running(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(params, context):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return "ok"

        tool = self.register(handler)
        tool.breaker.record_failure(tool.breaker.acquire())
        probe = asyncio.create_task(self.manager.call(tool.name, {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            blocked = await asyncio.gather(*(self.manager.call(tool.name, {}) for _ in range(20)))
            self.assertTrue(all(not result.success for result in blocked))
            self.assertEqual(calls, 1)
            release.set()
            self.assertTrue((await probe).success)
            self.assertEqual(tool.breaker.state, CircuitState.CLOSED)
            self.assertTrue((await self.manager.call(tool.name, {})).success)
        finally:
            release.set()
            await probe

    async def test_probe_exception_reopens_breaker(self):
        async def handler(params, context):
            raise RuntimeError("simulated tool failure")

        tool = self.register(handler)
        tool.breaker.record_failure(tool.breaker.acquire())
        result = await self.manager.call(tool.name, {})
        self.assertFalse(result.success)
        self.assertEqual(tool.breaker.state, CircuitState.OPEN)

    async def test_probe_timeout_reopens_breaker(self):
        async def handler(params, context):
            await asyncio.Event().wait()

        tool = self.register(handler, timeout_s=0.02)
        tool.breaker.record_failure(tool.breaker.acquire())
        result = await self.manager.call(tool.name, {})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "执行超时")
        self.assertEqual(tool.breaker.state, CircuitState.OPEN)

    async def test_probe_cancellation_propagates_and_allows_future_recovery(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(params, context):
            entered.set()
            await release.wait()
            return "ok"

        tool = self.register(handler)
        tool.breaker.record_failure(tool.breaker.acquire())
        probe = asyncio.create_task(self.manager.call(tool.name, {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            probe.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await probe
            self.assertEqual(tool.breaker.state, CircuitState.OPEN)
            self.assertEqual(tool.stats.failed, 0)
            release.set()
            self.assertTrue((await self.manager.call(tool.name, {})).success)
            self.assertEqual(tool.breaker.state, CircuitState.CLOSED)
        finally:
            release.set()
            if not probe.done():
                probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)

    async def test_normal_request_cancellation_does_not_open_breaker(self):
        entered = asyncio.Event()

        async def handler(params, context):
            entered.set()
            await asyncio.Event().wait()

        tool = self.register(handler)
        task = asyncio.create_task(self.manager.call(tool.name, {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(tool.breaker.state, CircuitState.CLOSED)
            self.assertEqual(tool.breaker.fail_count, 0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_old_inflight_success_cannot_finish_new_probe(self):
        old_entered, old_release = asyncio.Event(), asyncio.Event()
        probe_entered, probe_release = asyncio.Event(), asyncio.Event()

        async def handler(params, context):
            if params.get("kind") == "fail":
                raise RuntimeError("open breaker")
            entered, release = ((old_entered, old_release) if params.get("kind") == "old"
                                else (probe_entered, probe_release))
            entered.set()
            await release.wait()
            return "ok"

        tool = self.register(handler)
        old_task = asyncio.create_task(self.manager.call(tool.name, {"kind": "old"}))
        probe = None
        try:
            await asyncio.wait_for(old_entered.wait(), 2)
            self.assertFalse((await self.manager.call(tool.name, {"kind": "fail"})).success)
            probe = asyncio.create_task(self.manager.call(tool.name, {"kind": "probe"}))
            await asyncio.wait_for(probe_entered.wait(), 2)
            old_release.set()
            self.assertTrue((await old_task).success)
            self.assertEqual(tool.breaker.state, CircuitState.HALF_OPEN)
            self.assertFalse((await self.manager.call(tool.name, {})).success)
            probe_release.set()
            self.assertTrue((await probe).success)
            self.assertEqual(tool.breaker.state, CircuitState.CLOSED)
        finally:
            old_release.set()
            probe_release.set()
            await asyncio.gather(*(t for t in (old_task, probe) if t is not None), return_exceptions=True)

    async def test_fallback_does_not_treat_rejected_request_as_probe_success(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(params, context):
            entered.set()
            await release.wait()
            return "ok"

        tool = self.register(handler, fallback=lambda p, c, e: "fallback")
        tool.breaker.record_failure(tool.breaker.acquire())
        probe = asyncio.create_task(self.manager.call(tool.name, {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            result = await self.manager.call(tool.name, {})
            self.assertTrue(result.success)
            self.assertEqual(result.data, "fallback")
            self.assertEqual(tool.breaker.state, CircuitState.HALF_OPEN)
        finally:
            release.set()
            await probe


if __name__ == "__main__":
    unittest.main()
