"""Invalid caller input must not be counted as a tool outage."""

import asyncio
import unittest

from mcp.tool_manager import CircuitBreaker, CircuitState, MCPToolManager, Tool


class ToolValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = object.__new__(MCPToolManager)
        self.manager._tools = {}
        self.manager._cache = {}
        self.calls = 0

        async def handler(params, context):
            self.calls += 1
            return params["count"]

        self.tool = Tool(
            "count_tool", "offline validation test", handler,
            {"required": ["count"], "properties": {"count": {"type": "integer"}}},
        )
        self.tool.breaker = CircuitBreaker(failure_threshold=2, recovery_s=60)
        self.manager.register(self.tool)

    async def test_invalid_requests_do_not_trip_breaker_or_reduce_success_rate(self):
        for _ in range(5):
            result = await self.manager.call(self.tool.name, {})
            self.assertFalse(result.success)
            self.assertIn("缺少必需参数", result.error)

        self.assertEqual(self.tool.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.tool.breaker.fail_count, 0)
        self.assertEqual(self.tool.stats.validation_failed, 5)
        self.assertEqual(self.tool.stats.total, 0)
        self.assertEqual(self.calls, 0)

        result = await self.manager.call(self.tool.name, {"count": 3})
        self.assertTrue(result.success)
        self.assertEqual(result.data, 3)
        self.assertEqual(self.manager.get_stats()[self.tool.name]["success_rate"], 1.0)

    async def test_bool_is_not_accepted_as_integer(self):
        result = await self.manager.call(self.tool.name, {"count": True})
        self.assertFalse(result.success)
        self.assertIn("类型错误", result.error)
        self.assertEqual(self.tool.stats.validation_failed, 1)
        self.assertEqual(self.calls, 0)

    async def test_invalid_request_does_not_consume_half_open_probe(self):
        self.tool.breaker.record_failure(self.tool.breaker.acquire())
        self.tool.breaker.record_failure(self.tool.breaker.acquire())
        self.tool.breaker.opened_at -= 61

        invalid = await self.manager.call(self.tool.name, {})
        self.assertFalse(invalid.success)
        self.assertEqual(self.tool.breaker.state, CircuitState.OPEN)

        valid = await self.manager.call(self.tool.name, {"count": 2})
        self.assertTrue(valid.success)
        self.assertEqual(self.tool.breaker.state, CircuitState.CLOSED)

    async def test_handler_timeout_still_counts_as_tool_failure(self):
        async def wait_forever(params, context):
            await asyncio.Event().wait()

        self.tool.handler = wait_forever
        self.tool.timeout_s = 0.01
        result = await self.manager.call(self.tool.name, {"count": 1})
        self.assertFalse(result.success)
        self.assertEqual(self.tool.stats.failed, 1)
        self.assertEqual(self.tool.breaker.fail_count, 1)


if __name__ == "__main__":
    unittest.main()
