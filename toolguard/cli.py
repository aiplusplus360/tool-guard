"""CLI: demonstrate the guard on a scripted runaway agent.

    tool-guard demo
    tool-guard demo --max-repeats 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional, Sequence

from . import __version__
from .guard import Limits, ToolGuard
from .limits import ToolGuardError


def _demo_tools(guard: ToolGuard) -> None:
    @guard.tool
    def search(query: str) -> str:
        return f"3 results for {query!r}"

    @guard.tool
    def fetch(url: str) -> str:
        time.sleep(0.01)
        return f"<html>{url}</html>"

    @guard.tool
    def slow(query: str) -> str:
        time.sleep(2.0)
        return "finally"

    @guard.tool
    def broken(query: str) -> str:
        raise ValueError("upstream returned 500")


def cmd_demo(args: argparse.Namespace) -> int:
    guard = ToolGuard(
        limits=Limits(
            max_calls=args.max_calls,
            max_seconds=args.max_seconds,
            call_timeout_s=args.call_timeout,
            max_repeats=args.max_repeats,
        )
    )
    _demo_tools(guard)

    print("allowed:", guard.allowed_tools)
    print()

    scenarios = [
        ("normal call", lambda: guard.call("search", {"query": "pricing"})),
        ("tool raises", lambda: guard.call("broken", {"query": "x"})),
        ("timeout", lambda: guard.call("slow", {"query": "x"})),
        ("same call again", lambda: guard.call("search", {"query": "pricing"})),
        ("and again", lambda: guard.call("search", {"query": "pricing"})),
        ("and again", lambda: guard.call("search", {"query": "pricing"})),
        ("not allowed", lambda: guard.call("rm_rf", {"path": "/"})),
    ]

    for label, action in scenarios:
        try:
            value = action()
            print(f"  ok    {label:<16} {value!r}")
        except ToolGuardError as exc:
            # A limit was hit.
            print(f"  {exc.kind:<18} {label:<16} {exc.message}")
        except Exception as exc:  # noqa: BLE001
            # The tool itself failed. The guard deliberately does not swallow
            # this — a bug in a tool must stay a bug, not become a guard error.
            print(f"  {type(exc).__name__:<18} {label:<16} {exc}")

    print()
    print(json.dumps(guard.summary(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tool-guard",
        description="Sandbox agent tool calls: timeouts, allowlists, budgets, loop detection.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the scripted runaway agent")
    demo.add_argument("--max-calls", type=int, default=10)
    demo.add_argument("--max-seconds", type=float, default=30.0)
    demo.add_argument("--call-timeout", type=float, default=0.5)
    demo.add_argument("--max-repeats", type=int, default=2)
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
