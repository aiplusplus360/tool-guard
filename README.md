# tool-guard

**Sandbox an agent's tool calls: per-call timeouts, allowlists, call/time/cost budgets, and loop detection. The limits are enforced at the only entry point, so they're real.**

[![CI](https://github.com/aiplusplus360/tool-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/aiplusplus360/tool-guard/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```console
$ tool-guard demo

allowed: ['broken', 'fetch', 'search', 'slow']

  ok    normal call      "3 results for 'pricing'"
  ValueError         tool raises      upstream returned 500
  tool_timeout       timeout          tool 'slow' exceeded its 0.5s time limit and was abandoned.
  ok    same call again  "3 results for 'pricing'"
  loop_detected      and again        loop detected: 'search' called 3 times with identical arguments.
                                     You are repeating yourself — change approach or stop.
  tool_not_allowed   not allowed      tool 'rm_rf' is not allowed in this run.
```

---

## Why this exists

An agent loop with a tool that silently fails will call that tool forever. It is not a hypothetical: it is the single most common way an unattended agent turns a 2-cent job into a $400 bill and a rate-limit ban.

The usual mitigations don't work:

- **A prompt that says "don't loop"** is a suggestion, not a limit.
- **A `max_steps` on the orchestrator** misses the case where one step fans out.
- **A timeout on the HTTP client** doesn't stop a tool that returns quickly with an error.

What actually stops it is a check that runs *before* every call and cannot be bypassed. That is what `ToolGuard` is: the only way to invoke a registered tool.

## Install

No dependencies.

```bash
pip install git+https://github.com/aiplusplus360/tool-guard
```

## Usage

```python
from toolguard import Limits, ToolGuard

guard = ToolGuard(
    limits=Limits(
        max_calls=40,          # hard stop on runaway fan-out
        max_seconds=60,        # wall clock for the whole run
        max_cost=0.25,         # spend cap, using your own cost_fn
        call_timeout_s=10,     # no single tool call may hang
        max_repeats=3,         # same tool + same args, 3 times max
    ),
    allow=["search", "fetch", "summarise"],
    cost_fn=lambda tool, args: 0.001,
)

@guard.tool
def search(query: str) -> str:
    ...

result = guard.call("search", {"query": "pricing"})   # dict, the agent's shape
result = guard.call("search", query="pricing")        # kwargs, the human's shape
```

Violations raise a `ToolGuardError` subclass carrying a message written for a **model** to read:

```
loop detected: 'search' called 3 times with identical arguments.
You are repeating yourself — change approach or stop.
```

Not `GuardError: code 4031`. An agent can act on a sentence.

### Audit and summary

```python
print(guard.summary())
```

```json
{
  "calls": 4, "ok": 2, "failed": 2, "recorded": 4,
  "cost": 0.004, "elapsed_s": 0.5,
  "remaining_calls": 36,
  "slowest": "slow", "slowest_s": 0.5,
  "by_tool": {
    "search": {"calls": 2, "failed": 0, "seconds": 0.0},
    "broken": {"calls": 1, "failed": 1, "seconds": 0.0},
    "slow":   {"calls": 1, "failed": 1, "seconds": 0.5}
  }
}
```

`ok + failed == calls` always holds — every attempted call produces a record, including one that timed out.

## The five limits

| Limit | Stops |
|---|---|
| `max_calls` | Unbounded fan-out across a step |
| `max_seconds` | A run that never converges |
| `max_cost` | Spend, via your own `cost_fn` |
| `call_timeout_s` | One tool that never returns |
| `max_repeats` | The identical call, over and over |

Plus `allow` / `deny`, checked before anything else so the error is the useful one.

### Loop detection

Calls are hashed by `tool + sorted(args)`, so `{"a":1,"b":2}` and `{"b":2,"a":1}` are correctly identified as the same call. Arguments are truncated at 200 characters before hashing — a 300KB document and the same 300KB document are "the same call", and hashing the full payload on every iteration costs real time in the exact hot loop you're trying to escape.

## Design notes

**Tool exceptions propagate; they are not converted to guard errors.** If a tool raises `ValueError`, callers get a `ValueError`. Wrapping it in a guard error would make `except ToolGuardError` accidentally swallow application bugs — the opposite of what a safety layer should do.

**A rejected call does not consume budget.** Hitting `max_calls` and being told you're out of calls shouldn't also use up a call.

**Every attempted call is recorded, including timeouts.** An earlier version incremented the counter but skipped the audit record for a timed-out call, so `calls` said 4 while the log held 3 and the summary silently stopped reconciling. Now the summary reports `recorded` explicitly so the invariant can be checked.

**Timeouts are cooperative, and the README says so.** A Python thread cannot be forcibly killed. `call_timeout_s` abandons the call and raises; the underlying work may keep running. For hard isolation, run the tool in a subprocess. Claiming otherwise would be the kind of documentation that gets someone paged.

**Counting is lock-protected.** Two threads sharing a guard must not lose increments — tested with 40 concurrent calls.

**`None` means unlimited**, which is right for a local script and wrong for anything an agent can reach. The defaults are permissive because a guard that breaks a working script on import gets removed.

## Development

```bash
python -m unittest discover -s tests -v   # 36 tests
```

CI runs on Python 3.9, 3.11 and 3.13, and asserts each limit independently: the demo trips the timeout, the loop detector and the allowlist in one run, and the summary reconciles.

## License

[MIT](LICENSE)
