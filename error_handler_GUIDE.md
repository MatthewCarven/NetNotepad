# Python ErrorHandler - Standard Guide

## Overview

`describe_error(exc)` is a single function that introspects a Python exception and returns an `ErrorReport` object with three output flavors. Designed to live inside `except` clauses.

**Core promise: `describe_error` never raises.** If introspection fails partway through, the report records the failure in `partial_failures` and carries on. If the handler itself collapses entirely, the report falls back to `repr(exc)` and `type(exc).__name__`.

## API

```python
def describe_error(
    exc: BaseException | None = None,
    *,
    include_locals: bool = False,
    max_chain_depth: int = 10,
) -> ErrorReport:
    ...
```

### Parameters

- **`exc`** - the exception to describe. If `None`, falls back to `sys.exc_info()[1]` so you can call `describe_error()` bare inside an `except` block.
- **`include_locals`** - when `True`, captures `frame.f_locals` for each traceback frame. Each value is passed through a truncating, repr-safe representation. Off by default because frame locals can contain secrets that shouldn't leak into logs.
- **`max_chain_depth`** - hard cap on `__cause__` / `__context__` chain following. Defaults to 10. Guards against pathological or cyclic chains.

### Return type

```python
@dataclass
class ErrorReport:
    data: dict

    def to_dict(self) -> dict
    def to_string(self) -> str       # also via __str__
    def for_claude(self) -> str
```

## Output dict schema

```python
{
    "type": "ValueError",
    "module": "builtins",
    "message": "invalid literal for int() with base 10: 'abc'",
    "repr": "ValueError(\"invalid literal for int()...\")",
    "args": ("...",),                # safe-repr'd tuple of args
    "notes": [],                     # __notes__ list (Python 3.11+)
    "extra_attrs": {},               # non-dunder attrs from __dict__ if any
    "type_specific": {...},          # from dispatch table (see below)
    "traceback": [                   # frames in chronological order
        {
            "file": "...",
            "line": 42,
            "function": "parse",
            "code": "int(x)",
            # "locals": {...}        # only when include_locals=True
        },
        ...
    ],
    "chain": [                       # cause/context chain, nearest-to-oldest
        {
            "relation": "cause",     # or "context"
            "type": "KeyError",
            ...                      # same shape minus its own chain key
        },
        ...
    ],
    "partial_failures": [            # what (if anything) couldn't be captured
        {"step": "message", "error": "RuntimeError('...')"},
        ...
    ],
}
```

Alt paths (rare):

- `{"no_active_exception": True, ...}` - `describe_error()` called with no arg and no active exception.
- `{"error_handler_failed": True, "fallback_repr": ..., "fallback_type": ..., "handler_failure": ...}` - the worst-case fallback if the handler itself imploded.

## Output flavors

### `to_dict()`

Returns the structured dict above. Use for JSON logs, metrics, custom formatters.

### `to_string()` / `__str__`

Concise, traceback-style output. Matches Python's chained-exception printing convention: oldest exception first, relation phrases between each link, primary exception last. Drops into `log.error(report)` and f-strings naturally because `__str__` delegates here.

### `for_claude()`

Heavy / labeled / LLM-friendly. Section headers (`PRIMARY EXCEPTION`, `CAUSE / CONTEXT CHAIN`, `INTERNAL CAPTURE ISSUES`, etc.) and every field explicitly named. Chain ordered nearest-to-oldest rather than chronological - structured data view for an LLM to navigate, not a narrative.

## Common usage patterns

### Wrapping `main()`

```python
import sys
from error_handler import describe_error

def main():
    ...

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(describe_error(e), file=sys.stderr)
        sys.exit(1)
```

### Inside a request handler

```python
def handle_request(req):
    try:
        return do_work(req)
    except Exception as e:
        report = describe_error(e)
        log.error(str(report))
        metrics.send({"crash": report.to_dict()})
        return error_response(500)
```

### Sending a crash to Claude for diagnosis

```python
try:
    main()
except Exception as e:
    with open("crash_report.txt", "w") as f:
        f.write(describe_error(e, include_locals=True).for_claude())
    # Paste crash_report.txt into a Claude chat.
```

## Type-specific extractors

The dispatch table maps exception classes to extractor functions. Lookup walks the type's MRO, so registering for a base type automatically covers subclasses.

### Built-in extractors

| Exception | Fields extracted |
|---|---|
| `OSError` | errno, strerror, filename, filename2, winerror |
| `SyntaxError` | msg, filename, lineno, offset, text, end_lineno, end_offset |
| `AttributeError` | name, obj (Python 3.10+) |
| `KeyError` | missing_key |
| `UnicodeError` | encoding, start, end, reason, object_repr |

Because dispatch walks MRO, `FileNotFoundError` uses the `OSError` extractor automatically, `UnicodeDecodeError` uses the `UnicodeError` extractor, etc.

### Registering your own

```python
from error_handler import _register, _safe_repr


class APIError(Exception):
    def __init__(self, status_code, response_body):
        super().__init__("API returned " + str(status_code))
        self.status_code = status_code
        self.response_body = response_body


@_register(APIError)
def _extract_apierror(e):
    return {
        "status_code": getattr(e, "status_code", None),
        "response_body_preview": _safe_repr(getattr(e, "response_body", None), max_len=500),
    }
```

The extractor returns a dict that becomes the exception's `type_specific` block. Fields whose value is `None` get filtered out of the concise display automatically, so it's fine to default-to-None for missing attributes.

## Safety guarantees

1. **`describe_error` never raises.** Wrapped in an outermost try/except returning a fallback dict with `error_handler_failed: True` if everything else somehow blows up.

2. **Per-step safety nets.** Every introspection step (type, repr, str, args, notes, locals, traceback walk, chain walk, dispatch) runs through `_safe_capture` which catches any raise, records it to `partial_failures`, and returns a fallback value. One broken `__repr__` doesn't kill the whole report.

3. **Bounded chain walks.** Cycles detected via `id()`-based visited set. Depth capped at `max_chain_depth`. Truncation markers preserved in the chain so the reader knows what was cut off and why.

4. **Truncated reprs.** `_safe_repr` caps each value at 200 characters by default, with the original length preserved in the truncation marker. A frame with a 50KB list as a local won't produce a 50KB log entry.

5. **Hostile-object survival.** Passing a non-`BaseException` object (or an object whose `__repr__`, `__str__`, and `__class__` all raise) still produces a usable report rather than propagating the failure.

## When to use which output flavor

| Flavor | Best for |
|---|---|
| `str(report)` / `report.to_string()` | log files, stderr, exception emails - reads like a Python traceback with extras |
| `report.to_dict()` | metrics pipelines, JSON log aggregators, custom formatters |
| `report.for_claude()` | debugging with an LLM, crash report files for support tickets, anywhere context-free comprehension matters |

## Performance notes

Cheap by default. Dispatch is O(MRO depth), typically 3-5. Chain walk is bounded. Traceback walk is linear in frame count. The only operation that gets meaningfully expensive is `include_locals=True` on deep stacks - each frame triggers a `dict(f_locals)` plus one `_safe_repr` per value. Avoid in hot paths; use it deliberately when debugging.

## Tests

- **`test_error_handler.py`** - 20 unittest assertions covering the contract. Run with `python -m unittest test_error_handler.py`.
- **`test_chain.py`, `test_dispatch.py`, `test_locals.py`, `test_formatter.py`, `test_heavy.py`** - runnable visual examples / smoke tests showing actual output for various scenarios. Useful when debugging, adding new behavior, or showing teammates what the output looks like.

## Module exports

Public API:

- `describe_error(exc=None, *, include_locals=False, max_chain_depth=10) -> ErrorReport`
- `ErrorReport` (dataclass with `to_dict`, `to_string`, `for_claude`, `__str__`)

Internal but available for extension:

- `_register(exc_type)` - decorator to add a type-specific extractor to the dispatch table
- `_safe_repr(value, max_len=200)` - repr-safe, truncating repr helper (useful when writing custom extractors)
- `_safe_capture(label, fn, default, failures)` - the safety chokepoint (rarely needed externally)

The underscore prefix on `_register` etc. signals "internal but stable" - they're documented here and tested through the public API, so use them confidently when extending the dispatch table.

