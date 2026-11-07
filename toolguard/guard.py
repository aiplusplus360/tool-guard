"""The guard: wrap tools, enforce limits, record everything.

A single agent run gets a `ToolGuard`. The guard is the only way to invoke a
tool, which is what makes the limits real rather than advisory.
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .limits import (
    BudgetExhausted,
    CallRecord,
    LoopDetected,
    ToolGuardError,
    ToolNotAllowed,
    ToolTimeout,
)


@dataclass
class Limits:
    """Everything a run is allowed to spend.

    `None` means unlimited for each field, which is the correct default for a
    local script and the wrong one for anything reachable by an agent.
    """

    max_calls: Optional[int] = None
    max_seconds: Optional[float] = None
    max_cost: Optional[float] = None
    call_timeout_s: Optional[float] = None
    max_repeats: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("max_calls", "max_seconds", "max_cost", "call_timeout_s", "max_repeats"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


def _signature(tool: str, arguments: Dict[str, Any]) -> str:
    """A stable identity for a call, used for loop detection.

    Sorting keys makes `{"a":1,"b":2}` and `{"b":2,"a":1}` the same call —
    which they are.
    """
    parts = []
    for key in sorted(arguments):
        value = arguments[key]
        # Large payloads are truncated: two calls with a 300KB document are
        # "the same call" for loop purposes, and hashing the whole thing on
        # every call costs real time in a hot loop.
        rendered = repr(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "..."
        parts.append(f"{key}={rendered}")
    return f"{tool}({','.join(parts)})"


class ToolGuard:
    """A per-run sandbox around a set of tool functions."""

    def __init__(
        self,
        *,
        limits: Optional[Limits] = None,
        allow: Optional[Sequence[str]] = None,
        deny: Optional[Sequence[str]] = None,
        cost_fn: Optional[Callable[[str, Dict[str, Any]], float]] = None,
    ) -> None:
        self.limits = limits or Limits()
        self._allow: Optional[Set[str]] = set(allow) if allow is not None else None
        self._deny: Set[str] = set(deny or ())
        self._tools: Dict[str, Callable[..., Any]] = {}
        self._cost_fn = cost_fn

        self.calls = 0
        self.cost = 0.0
        self.started = time.monotonic()
        self.audit: List[CallRecord] = []
        self._counts: Dict[str, int] = {}
        # Guards are used from one thread in practice, but two threads sharing
        # one guard must not corrupt the counters.
        self._lock = threading.Lock()

    # ---------------------------------------------------------- registration --

    def tool(self, func: Optional[Callable[..., Any]] = None, *, name: Optional[str] = None):
        """Register a function. Registered tools are still subject to `allow`."""

        def register(handler: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or handler.__name__
            if tool_name in self._tools:
                raise ValueError(f"duplicate tool: {tool_name!r}")
            self._tools[tool_name] = handler
            return handler

        if func is not None:
            return register(func)
        return register

    @property
    def allowed_tools(self) -> List[str]:
        """The tools actually callable in this run."""
        if self._allow is not None:
            names = sorted(self._allow & self._tools.keys())
        else:
            names = sorted(self._tools.keys())
        return [n for n in names if n not in self._deny]

    # -------------------------------------------------------------- enforce --

    def _check_allowed(self, tool: str) -> None:
        if tool in self._deny:
            raise ToolNotAllowed(tool, self.allowed_tools)
        if self._allow is not None and tool not in self._allow:
            raise ToolNotAllowed(tool, self.allowed_tools)
        if tool not in self._tools:
            raise ToolNotAllowed(tool, self.allowed_tools)

    def _check_budgets(self) -> None:
        limits = self.limits
        if limits.max_calls is not None and self.calls >= limits.max_calls:
            raise BudgetExhausted("calls", self.calls, limits.max_calls)
        if limits.max_seconds is not None:
            elapsed = time.monotonic() - self.started
            if elapsed >= limits.max_seconds:
                raise BudgetExhausted(
                    "seconds", round(elapsed, 3), limits.max_seconds
                )
        if limits.max_cost is not None and self.cost >= limits.max_cost:
            raise BudgetExhausted("cost", round(self.cost, 6), limits.max_cost)

    def _check_loop(self, tool: str, arguments: Dict[str, Any]) -> None:
        if self.limits.max_repeats is None:
            return
        signature = _signature(tool, arguments)
        repeats = self._counts.get(signature, 0) + 1
        self._counts[signature] = repeats
        if repeats > self.limits.max_repeats:
            raise LoopDetected(tool, repeats, signature)

    # ----------------------------------------------------------------- call --

    def call(
        self, tool: str, arguments: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Any:
        """Invoke a tool under every limit.

        Arguments may be passed as a dict (the shape an agent produces) or as
        keyword arguments (the shape a human writes).
        """
        merged: Dict[str, Any] = dict(arguments or {})
        if kwargs:
            overlap = merged.keys() & kwargs.keys()
            if overlap:
                raise TypeError(f"arguments given twice: {sorted(overlap)}")
            merged.update(kwargs)

        # Order matters: an unknown tool is a clearer error than "budget spent".
        self._check_allowed(tool)
        self._check_budgets()
        self._check_loop(tool, merged)

        handler = self._tools[tool]
        estimated_cost = self._cost_fn(tool, merged) if self._cost_fn else 0.0

        started = time.monotonic()
        with self._lock:
            self.calls += 1
            self.cost += estimated_cost

        try:
            result = self._invoke(handler, tool, merged)
        except BaseException as exc:
            # Every attempted call gets a record, including a timeout or a
            # guard violation raised mid-call. Otherwise `calls` and `audit`
            # disagree and the summary stops reconciling.
            with self._lock:
                self.audit.append(
                    CallRecord(
                        tool=tool,
                        ok=False,
                        duration_s=time.monotonic() - started,
                        cost=estimated_cost,
                        error=f"{type(exc).__name__}: {exc}",
                        arguments=merged,
                    )
                )
            raise

        with self._lock:
            self.audit.append(
                CallRecord(
                    tool=tool,
                    ok=True,
                    duration_s=time.monotonic() - started,
                    cost=estimated_cost,
                    arguments=merged,
                )
            )
        return result

    def _invoke(self, handler: Callable[..., Any], tool: str, arguments: Dict[str, Any]) -> Any:
        timeout = self.limits.call_timeout_s
        if timeout is None:
            return handler(**arguments)

        # A Python thread cannot be forcibly killed. The guard abandons the
        # call and raises a timeout, but the underlying work may keep running.
        # For hard isolation, run the tool in a subprocess — this is a
        # cooperative limit, and pretending otherwise would be dishonest.
        outcome: Dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = handler(**arguments)
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True, name=f"toolguard:{tool}")
        thread.start()
        thread.join(timeout)

        if thread.is_alive():
            raise ToolTimeout(tool, timeout)
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    # ---------------------------------------------------------------- report --

    def summary(self) -> Dict[str, Any]:
        """Rollups that must reconcile: ok + failed == calls."""
        elapsed = time.monotonic() - self.started
        failures = [record for record in self.audit if not record.ok]
        slowest = max(self.audit, key=lambda r: r.duration_s, default=None)
        return {
            "calls": self.calls,
            "ok": len(self.audit) - len(failures),
            "failed": len(failures),
            "recorded": len(self.audit),
            "cost": round(self.cost, 6),
            "elapsed_s": round(elapsed, 3),
            "remaining_calls": (
                None
                if self.limits.max_calls is None
                else max(0, self.limits.max_calls - self.calls)
            ),
            "slowest": slowest.tool if slowest else None,
            "slowest_s": round(slowest.duration_s, 4) if slowest else 0.0,
            "by_tool": self._by_tool(),
        }

    def _by_tool(self) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        for record in self.audit:
            entry = grouped.setdefault(
                record.tool, {"calls": 0, "failed": 0, "seconds": 0.0}
            )
            entry["calls"] += 1
            entry["failed"] += 0 if record.ok else 1
            entry["seconds"] = round(entry["seconds"] + record.duration_s, 4)
        return grouped
