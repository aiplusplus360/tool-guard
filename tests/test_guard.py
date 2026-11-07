"""Tests for the guard.

The scenarios that matter are the ones that cost money at 3am: a loop that
never terminates, a tool that never returns, a run that keeps spending.
"""

import threading
import time
import unittest

from toolguard import (
    BudgetExhausted,
    Limits,
    LoopDetected,
    ToolGuard,
    ToolGuardError,
    ToolNotAllowed,
    ToolTimeout,
)


def build(**limit_kwargs):
    guard = ToolGuard(limits=Limits(**limit_kwargs))

    @guard.tool
    def echo(text: str) -> str:
        return text

    @guard.tool
    def add(a: int, b: int) -> int:
        return a + b

    @guard.tool
    def boom(text: str) -> str:
        raise ValueError("upstream failed")

    @guard.tool
    def sleep_for(seconds: float) -> str:
        time.sleep(seconds)
        return "done"

    return guard


class TestRegistration(unittest.TestCase):
    def test_tools_registered(self):
        guard = build()
        self.assertEqual(guard.allowed_tools, ["add", "boom", "echo", "sleep_for"])

    def test_duplicate_tool_rejected(self):
        guard = build()
        with self.assertRaises(ValueError):
            @guard.tool(name="echo")
            def other(text: str) -> str:
                return text

    def test_allowlist(self):
        guard = ToolGuard(allow=["echo"])

        @guard.tool
        def echo(text: str) -> str:
            return text

        @guard.tool
        def secret(text: str) -> str:
            return text

        self.assertEqual(guard.allowed_tools, ["echo"])
        self.assertEqual(guard.call("echo", {"text": "hi"}), "hi")
        with self.assertRaises(ToolNotAllowed):
            guard.call("secret", {"text": "hi"})

    def test_denylist_wins(self):
        guard = ToolGuard(allow=["echo"], deny=["echo"])

        @guard.tool
        def echo(text: str) -> str:
            return text

        self.assertEqual(guard.allowed_tools, [])
        with self.assertRaises(ToolNotAllowed):
            guard.call("echo", {"text": "hi"})

    def test_unknown_tool(self):
        guard = build()
        with self.assertRaises(ToolNotAllowed):
            guard.call("nonexistent", {})


class TestCalling(unittest.TestCase):
    def test_kwargs_form(self):
        guard = build()
        self.assertEqual(guard.call("add", a=2, b=3), 5)

    def test_dict_form(self):
        guard = build()
        self.assertEqual(guard.call("add", {"a": 2, "b": 3}), 5)

    def test_duplicate_arguments_rejected(self):
        guard = build()
        with self.assertRaises(TypeError):
            guard.call("add", {"a": 1}, a=2)

    def test_bad_arguments_propagate(self):
        guard = build()
        with self.assertRaises(TypeError):
            guard.call("add", {"a": 1})

    def test_tool_exception_propagates(self):
        guard = build()
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "x"})

    def test_failed_call_is_audited(self):
        guard = build()
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "x"})
        self.assertEqual(len(guard.audit), 1)
        self.assertFalse(guard.audit[0].ok)
        self.assertIn("ValueError", guard.audit[0].error)


class TestCallBudget(unittest.TestCase):
    def test_allows_up_to_limit(self):
        guard = build(max_calls=2)
        guard.call("echo", {"text": "1"})
        guard.call("echo", {"text": "2"})
        with self.assertRaises(BudgetExhausted):
            guard.call("echo", {"text": "3"})

    def test_error_reports_numbers(self):
        guard = build(max_calls=1)
        guard.call("echo", {"text": "1"})
        with self.assertRaises(BudgetExhausted) as ctx:
            guard.call("echo", {"text": "2"})
        self.assertEqual(ctx.exception.details["used"], 1)
        self.assertEqual(ctx.exception.details["limit"], 1)

    def test_zero_budget_blocks_everything(self):
        guard = build(max_calls=0)
        with self.assertRaises(BudgetExhausted):
            guard.call("echo", {"text": "x"})


class TestTimeBudget(unittest.TestCase):
    def test_elapsed_limit(self):
        guard = build(max_seconds=0.05)
        guard.call("echo", {"text": "1"})
        time.sleep(0.06)
        with self.assertRaises(BudgetExhausted) as ctx:
            guard.call("echo", {"text": "2"})
        self.assertEqual(ctx.exception.details["resource"], "seconds")


class TestCostBudget(unittest.TestCase):
    def test_cost_accumulates(self):
        guard = ToolGuard(
            limits=Limits(max_cost=0.02),
            cost_fn=lambda tool, args: 0.01,
        )

        @guard.tool
        def echo(text: str) -> str:
            return text

        guard.call("echo", {"text": "1"})
        guard.call("echo", {"text": "2"})
        with self.assertRaises(BudgetExhausted) as ctx:
            guard.call("echo", {"text": "3"})
        self.assertEqual(ctx.exception.details["resource"], "cost")

    def test_cost_recorded_in_summary(self):
        guard = ToolGuard(cost_fn=lambda tool, args: 0.005)

        @guard.tool
        def echo(text: str) -> str:
            return text

        guard.call("echo", {"text": "1"})
        self.assertAlmostEqual(guard.summary()["cost"], 0.005)


class TestTimeout(unittest.TestCase):
    def test_slow_call_times_out(self):
        guard = build(call_timeout_s=0.05)
        started = time.monotonic()
        with self.assertRaises(ToolTimeout):
            guard.call("sleep_for", {"seconds": 2.0})
        # Must return promptly, not wait for the tool.
        self.assertLess(time.monotonic() - started, 1.0)

    def test_fast_call_completes(self):
        guard = build(call_timeout_s=1.0)
        self.assertEqual(guard.call("sleep_for", {"seconds": 0.01}), "done")

    def test_tool_exception_inside_timeout_wrapper(self):
        guard = build(call_timeout_s=1.0)
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "x"})

    def test_timeout_is_not_a_generic_error(self):
        guard = build(call_timeout_s=0.01)
        with self.assertRaises(ToolGuardError) as ctx:
            guard.call("sleep_for", {"seconds": 1.0})
        self.assertEqual(ctx.exception.kind, "tool_timeout")


class TestLoopDetection(unittest.TestCase):
    def test_identical_calls_trip(self):
        guard = build(max_repeats=2)
        guard.call("echo", {"text": "same"})
        guard.call("echo", {"text": "same"})
        with self.assertRaises(LoopDetected) as ctx:
            guard.call("echo", {"text": "same"})
        self.assertEqual(ctx.exception.details["repeats"], 3)

    def test_different_arguments_do_not_trip(self):
        guard = build(max_repeats=2)
        guard.call("echo", {"text": "a"})
        guard.call("echo", {"text": "b"})
        guard.call("echo", {"text": "c"})
        # No exception: distinct calls are progress, not a loop.
        self.assertEqual(len(guard.audit), 3)

    def test_key_order_does_not_matter(self):
        # {"a":1,"b":2} and {"b":2,"a":1} are the same call.
        guard = build(max_repeats=1)
        guard.call("add", {"a": 1, "b": 2})
        with self.assertRaises(LoopDetected):
            guard.call("add", {"b": 2, "a": 1})

    def test_long_arguments_are_truncated(self):
        guard = build(max_repeats=1)
        big = "x" * 10_000
        guard.call("echo", {"text": big})
        # Truncation means a huge-but-identical payload still trips the loop
        # detector rather than silently getting a fresh budget.
        with self.assertRaises(LoopDetected):
            guard.call("echo", {"text": big})

    def test_loop_detection_off_by_default(self):
        guard = build()
        for _ in range(50):
            guard.call("echo", {"text": "same"})
        self.assertEqual(guard.calls, 50)

    def test_error_message_is_actionable(self):
        guard = build(max_repeats=0)
        with self.assertRaises(LoopDetected) as ctx:
            guard.call("echo", {"text": "x"})
        self.assertIn("repeating yourself", ctx.exception.message)


class TestAudit(unittest.TestCase):
    def test_records_every_call(self):
        guard = build()
        guard.call("echo", {"text": "1"})
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "2"})
        self.assertEqual(len(guard.audit), 2)

    def test_summary(self):
        guard = build()
        guard.call("echo", {"text": "1"})
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "2"})
        summary = guard.summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("echo", summary["by_tool"])

    def test_summary_remaining(self):
        guard = build(max_calls=5)
        guard.call("echo", {"text": "1"})
        self.assertEqual(guard.summary()["remaining_calls"], 4)

    def test_blocked_call_is_not_counted_as_used(self):
        guard = build(max_calls=2)
        guard.call("echo", {"text": "1"})
        guard.call("echo", {"text": "2"})
        with self.assertRaises(BudgetExhausted):
            guard.call("echo", {"text": "3"})
        # 2, not 3: a rejected call did not consume a slot.
        self.assertEqual(guard.calls, 2)

    def test_summary_reconciles_with_audit(self):
        # Every attempted call must produce a record, including a timeout,
        # or ok + failed stops equalling calls.
        guard = build(call_timeout_s=0.02)
        guard.call("echo", {"text": "1"})
        with self.assertRaises(ToolTimeout):
            guard.call("sleep_for", {"seconds": 1.0})
        with self.assertRaises(ValueError):
            guard.call("boom", {"text": "x"})
        summary = guard.summary()
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["recorded"], 3)
        self.assertEqual(summary["ok"] + summary["failed"], summary["calls"])


class TestLimits(unittest.TestCase):
    def test_negative_limit_rejected(self):
        with self.assertRaises(ValueError):
            Limits(max_calls=-1)

    def test_none_means_unlimited(self):
        guard = build()
        for index in range(100):
            guard.call("echo", {"text": str(index)})
        self.assertEqual(guard.calls, 100)

    def test_error_dict_shape(self):
        guard = build(max_calls=0)
        with self.assertRaises(BudgetExhausted) as ctx:
            guard.call("echo", {"text": "x"})
        payload = ctx.exception.to_dict()
        self.assertEqual(payload["kind"], "budget_exhausted")
        self.assertIn("details", payload)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_calls_do_not_lose_count(self):
        guard = build()

        def worker(index):
            try:
                guard.call("echo", {"text": str(index)})
            except ToolGuardError:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(guard.calls, 40)
        self.assertEqual(len(guard.audit), 40)


if __name__ == "__main__":
    unittest.main()
