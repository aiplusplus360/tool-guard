"""Limits and the errors they raise.

Every error here subclasses `ToolGuardError`, which carries a `kind` and a
`message` written to be *read by a model*, not just logged. When an agent hits
a limit, the useful thing is a sentence it can act on: "this tool is not
allowed" is actionable; "GuardError: 4031" is not.

None of these are `ValueError`s or `RuntimeError`s. Catching a guard violation
must be possible without accidentally catching a bug in the tool itself, which
is exactly what `except Exception` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ToolGuardError(Exception):
    """Base class for every limit the guard enforces."""

    kind = "guard_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        payload = {"kind": self.kind, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ToolNotAllowed(ToolGuardError):
    """The tool is not on the allowlist (or is explicitly denied)."""

    kind = "tool_not_allowed"

    def __init__(self, tool: str, allowed: List[str]) -> None:
        super().__init__(
            f"tool {tool!r} is not allowed in this run. "
            f"Available tools: {sorted(allowed) or 'none'}.",
            tool=tool,
            allowed=sorted(allowed),
        )


class ToolTimeout(ToolGuardError):
    """The call exceeded its time limit."""

    kind = "tool_timeout"

    def __init__(self, tool: str, limit_s: float) -> None:
        super().__init__(
            f"tool {tool!r} exceeded its {limit_s:g}s time limit and was abandoned. "
            "Try a narrower request or a different tool.",
            tool=tool,
            limit_s=limit_s,
        )


class BudgetExhausted(ToolGuardError):
    """The run has used up one of its allowances."""

    kind = "budget_exhausted"

    def __init__(self, resource: str, used: Any, limit: Any) -> None:
        super().__init__(
            f"run budget exhausted: {resource} used {used} of {limit}. "
            "Stop calling tools and answer with what you have.",
            resource=resource,
            used=used,
            limit=limit,
        )


class LoopDetected(ToolGuardError):
    """The same call repeated too many times — the classic runaway loop."""

    kind = "loop_detected"

    def __init__(self, tool: str, repeats: int, signature: str) -> None:
        super().__init__(
            f"loop detected: {tool!r} called {repeats} times with identical "
            "arguments. You are repeating yourself — change approach or stop.",
            tool=tool,
            repeats=repeats,
            signature=signature,
        )


@dataclass
class CallRecord:
    """One entry in the audit log."""

    tool: str
    ok: bool
    duration_s: float
    cost: float = 0.0
    error: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 4),
            "cost": round(self.cost, 6),
            "error": self.error,
        }
