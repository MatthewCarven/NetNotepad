"""
Python ErrorHandler - one function to surface everything knowable about an exception.

Usage inside an except clause:

    from error_handler import describe_error

    try:
        risky()
    except Exception as e:
        report = describe_error(e)
        log.error(report)                    # uses .to_string() via __str__
        log.error(report.for_claude())       # heavy / LLM-friendly edition (stub)
        send_to_metrics(report.to_dict())    # structured

Design contract: this function NEVER raises. If introspection of the exception
fails partway through, the returned report records the partial failure in
`partial_failures` and carries on. If the handler itself collapses entirely
(e.g. MemoryError mid-walk), the report falls back to the most primitive
description possible: repr(exc) and type(exc).__name__.
"""

from __future__ import annotations

import linecache
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Safety primitives
# ---------------------------------------------------------------------------

_REPR_MAX_LEN_DEFAULT = 200


def _safe_capture(label, fn, default, failures):
    """Run fn() and return its result. If it raises, record the failure in
    `failures` and return `default`. Single chokepoint for all introspection."""
    try:
        return fn()
    except BaseException as inner:
        try:
            failures.append({"step": label, "error": repr(inner)})
        except BaseException:
            pass
        return default


def _safe_repr(value, max_len=_REPR_MAX_LEN_DEFAULT):
    """repr(value) that survives broken __repr__ and truncates long output."""
    try:
        s = repr(value)
    except BaseException:
        try:
            return "<unrepresentable: " + type(value).__name__ + ">"
        except BaseException:
            return "<unrepresentable>"
    if len(s) > max_len:
        return s[:max_len] + "... [truncated, full len=" + str(len(s)) + "]"
    return s


# ---------------------------------------------------------------------------
# Return object
# ---------------------------------------------------------------------------

@dataclass
class ErrorReport:
    """Result of describe_error. Stringifies to the concise human-readable form
    by default so it drops into log.error(...) and f-strings cleanly.

    Three output flavors:
      to_dict()      structured, suitable for JSON / metrics pipelines
      to_string()    concise, traceback-style human format (also __str__)
      for_claude()   heavy / LLM-friendly edition (Task 9)
    """
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(self.data)

    def to_string(self):
        return _format_concise(self.data)

    def for_claude(self):
        return _format_heavy(self.data)

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        kind = self.data.get("type", "?")
        msg = self.data.get("message", "")
        return "ErrorReport(" + str(kind) + ": " + repr(msg) + ")"


# ---------------------------------------------------------------------------
# Task 2: Generic exception extractor
# ---------------------------------------------------------------------------

def _extract_notes(exc):
    """Return __notes__ as a list of strings, surviving non-iterable junk."""
    notes = getattr(exc, "__notes__", None)
    if notes is None:
        return []
    try:
        return [str(n) for n in notes]
    except TypeError:
        return [_safe_repr(notes)]


def _extra_attrs(exc):
    """Non-dunder attributes off the exception's __dict__, each safe-repr'd.
    Many built-in exceptions have no __dict__ - return {} in that case."""
    out = {}
    try:
        d = vars(exc)
    except TypeError:
        return out
    for name, value in d.items():
        if name.startswith("_"):
            continue
        out[name] = _safe_repr(value)
    return out


# ---------------------------------------------------------------------------
# Task 3: Traceback walker (no locals yet - Task 6 wires in the flag)
# ---------------------------------------------------------------------------

def _walk_traceback(exc, include_locals, failures):
    """Walk exc.__traceback__ linked list, oldest frame first. Each frame
    capture is wrapped so a single bad frame can't break the whole walk."""
    frames = []
    tb = getattr(exc, "__traceback__", None)
    while tb is not None:
        frame_data = _safe_capture(
            "frame",
            lambda tb=tb: _build_frame(tb, include_locals, failures),
            None,
            failures,
        )
        if frame_data is not None:
            frames.append(frame_data)
        tb = tb.tb_next
    return frames


def _build_frame(tb, include_locals, failures):
    """Extract a single traceback frame into a dict. When include_locals is
    True, frame.f_locals is captured with each value passed through _safe_repr
    (truncated and __repr__-safe). The whole locals grab is itself wrapped in
    _safe_capture so a pathological frame can't break frame extraction."""
    frame = tb.tb_frame
    code = frame.f_code
    filename = code.co_filename
    lineno = tb.tb_lineno
    function = code.co_name
    source = linecache.getline(filename, lineno).strip() or None
    out = {
        "file": filename,
        "line": lineno,
        "function": function,
        "code": source,
    }
    if include_locals:
        out["locals"] = _safe_capture(
            "frame_locals",
            lambda: {k: _safe_repr(v) for k, v in frame.f_locals.items()},
            {},
            failures,
        )
    return out


# ---------------------------------------------------------------------------
# Task 4: Chain walker with cycle and depth guards
# ---------------------------------------------------------------------------

def _walk_chain(exc, max_depth, include_locals, failures):
    """Follow __cause__ first, then __context__ (unless __suppress_context__).

    Returns links from nearest to oldest. Each link is the same shape as the
    top-level report minus its own `chain` key (to avoid infinite recursion).
    Cycles are detected via id()-based visited set; depth overflow and cycles
    are recorded as truncation markers in the chain itself."""
    chain = []
    visited = {id(exc)}
    current = exc
    depth = 0

    while depth < max_depth:
        nxt = None
        relation = None

        cause = getattr(current, "__cause__", None)
        if cause is not None:
            nxt, relation = cause, "cause"
        else:
            ctx = getattr(current, "__context__", None)
            suppressed = getattr(current, "__suppress_context__", False)
            if ctx is not None and not suppressed:
                nxt, relation = ctx, "context"

        if nxt is None:
            break

        if id(nxt) in visited:
            chain.append({
                "relation": relation,
                "truncated": "cycle_detected",
                "type": _safe_capture(
                    "chain.cycle.type",
                    lambda nxt=nxt: type(nxt).__name__,
                    "<unknown>",
                    failures,
                ),
            })
            break

        visited.add(id(nxt))

        link = _safe_capture(
            "chain.link",
            lambda nxt=nxt: _build_data(
                nxt, failures, max_depth,
                with_chain=False, include_locals=include_locals,
            ),
            {},
            failures,
        )
        link["relation"] = relation
        chain.append(link)

        current = nxt
        depth += 1

    # Hit the depth cap with more chain remaining?
    if depth >= max_depth:
        has_more = (
            getattr(current, "__cause__", None) is not None
            or (
                getattr(current, "__context__", None) is not None
                and not getattr(current, "__suppress_context__", False)
            )
        )
        if has_more:
            chain.append({"truncated": "max_depth_reached"})

    return chain


# ---------------------------------------------------------------------------
# Task 5: Type-specific dispatch table
# ---------------------------------------------------------------------------
#
# Maps exception class -> extractor function. Lookup walks the type's MRO so
# subclasses inherit (FileNotFoundError gets the OSError extractor for free).
# Each extractor must return a dict and should be robust to missing attributes
# (built-in exceptions are remarkably inconsistent about which attrs they set).

_TYPE_EXTRACTORS = {}


def _register(exc_type):
    """Decorator to register a type-specific extractor."""
    def deco(fn):
        _TYPE_EXTRACTORS[exc_type] = fn
        return fn
    return deco


@_register(OSError)
def _extract_oserror(e):
    return {
        "errno": getattr(e, "errno", None),
        "strerror": getattr(e, "strerror", None),
        "filename": getattr(e, "filename", None),
        "filename2": getattr(e, "filename2", None),
        "winerror": getattr(e, "winerror", None),
    }


@_register(SyntaxError)
def _extract_syntaxerror(e):
    return {
        "msg": getattr(e, "msg", None),
        "filename": getattr(e, "filename", None),
        "lineno": getattr(e, "lineno", None),
        "offset": getattr(e, "offset", None),
        "text": getattr(e, "text", None),
        "end_lineno": getattr(e, "end_lineno", None),
        "end_offset": getattr(e, "end_offset", None),
    }


@_register(AttributeError)
def _extract_attributeerror(e):
    out = {"name": getattr(e, "name", None)}
    if hasattr(e, "obj"):
        out["obj"] = _safe_repr(e.obj)
    return out


@_register(KeyError)
def _extract_keyerror(e):
    args = getattr(e, "args", ())
    return {"missing_key": _safe_repr(args[0]) if args else None}


@_register(UnicodeError)
def _extract_unicodeerror(e):
    out = {
        "encoding": getattr(e, "encoding", None),
        "start": getattr(e, "start", None),
        "end": getattr(e, "end", None),
        "reason": getattr(e, "reason", None),
    }
    if hasattr(e, "object"):
        out["object_repr"] = _safe_repr(e.object)
    return out


def _apply_dispatch(exc, failures):
    """Walk MRO of type(exc) and run the first matching extractor.
    Returns {} if no extractor matches (or if MRO lookup itself blew up)."""
    try:
        mro = type(exc).__mro__
    except BaseException as inner:
        try:
            failures.append({"step": "dispatch.mro", "error": repr(inner)})
        except BaseException:
            pass
        return {}
    for cls in mro:
        if cls in _TYPE_EXTRACTORS:
            extractor = _TYPE_EXTRACTORS[cls]
            return _safe_capture(
                "dispatch[" + cls.__name__ + "]",
                lambda exc=exc, extractor=extractor: extractor(exc),
                {},
                failures,
            )
    return {}


# ---------------------------------------------------------------------------
# Pipeline: build the full data dict for one exception
# ---------------------------------------------------------------------------

def _build_data(exc, failures, max_chain_depth=10, *, with_chain=True, include_locals=False):
    """Assemble the introspection dict for one exception.

    Called by describe_error (with_chain=True) for the primary exception and
    by _walk_chain (with_chain=False) for each chain link, so that links get
    full introspection without their own chains recursing. include_locals is
    threaded through to the traceback walker for both the primary and chain
    links so the flag applies uniformly to every frame in the report."""
    data = {}
    data["type"] = _safe_capture("type", lambda: type(exc).__name__, "<unknown>", failures)
    data["module"] = _safe_capture("module", lambda: type(exc).__module__, "<unknown>", failures)
    data["message"] = _safe_capture("message", lambda: str(exc), "<unrenderable>", failures)
    data["repr"] = _safe_capture("repr", lambda: repr(exc), "<unrepresentable>", failures)
    data["args"] = _safe_capture(
        "args",
        lambda: tuple(_safe_repr(a) for a in getattr(exc, "args", ())),
        (),
        failures,
    )
    data["notes"] = _safe_capture("notes", lambda: _extract_notes(exc), [], failures)
    data["extra_attrs"] = _safe_capture("extra_attrs", lambda: _extra_attrs(exc), {}, failures)
    data["type_specific"] = _apply_dispatch(exc, failures)
    data["traceback"] = _safe_capture(
        "traceback",
        lambda: _walk_traceback(exc, include_locals, failures),
        [],
        failures,
    )
    if with_chain:
        data["chain"] = _safe_capture(
            "chain",
            lambda: _walk_chain(exc, max_chain_depth, include_locals, failures),
            [],
            failures,
        )
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def describe_error(exc=None, *, include_locals=False, max_chain_depth=10):
    """Inspect an exception and return an ErrorReport. NEVER raises.

    Args:
        exc: the exception to describe; if None, falls back to sys.exc_info()
        include_locals: capture frame.f_locals (Task 6 wires this up)
        max_chain_depth: cap on cause/context chain walking (Task 4 wires this up)
    """
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return ErrorReport({
                "error_handler_failed": False,
                "no_active_exception": True,
            })

        failures = []
        data = _build_data(
            exc, failures, max_chain_depth, include_locals=include_locals,
        )
        data["partial_failures"] = failures
        return ErrorReport(data)

    except BaseException as handler_failure:
        try:
            fallback_repr = repr(exc)
        except BaseException:
            fallback_repr = "<repr unavailable>"
        try:
            fallback_type = type(exc).__name__ if exc is not None else "<no exc>"
        except BaseException:
            fallback_type = "<type unavailable>"
        try:
            handler_failure_repr = repr(handler_failure)
        except BaseException:
            handler_failure_repr = "<handler failure unrepresentable>"
        return ErrorReport({
            "error_handler_failed": True,
            "fallback_repr": fallback_repr,
            "fallback_type": fallback_type,
            "handler_failure": handler_failure_repr,
        })


# ---------------------------------------------------------------------------
# Task 7: Concise formatter (heavy edition still stubbed - Task 9)
# ---------------------------------------------------------------------------

def _format_concise(data):
    """Concise, traceback-style human output. Matches Python's chained-exception
    printing convention: oldest exception first, relation phrases between each
    link, primary exception last.

    Per-exception layout:
      Traceback (most recent call last):
        File "...", line N, in func
          source line
            local_name = value      (only when include_locals was True)
      module.ExceptionType: message
        [type_specific_key=value, ...]
        note: ...

    Partial failures inside error_handler are reported at the end so they
    don't disrupt the reading flow of the actual exception."""
    if data.get("error_handler_failed"):
        return (
            "[error_handler failed]\n"
            "  original: " + str(data.get("fallback_type", "?")) + " "
            + str(data.get("fallback_repr", "?")) + "\n"
            "  handler:  " + str(data.get("handler_failure", "?"))
        )
    if data.get("no_active_exception"):
        return "[no active exception]"

    lines = []

    # Chain is walked newest-to-oldest; reverse so oldest prints first
    # (matches Python's traceback module convention).
    chain = list(reversed(data.get("chain") or []))
    for link in chain:
        if link.get("truncated"):
            marker = link["truncated"]
            note = (
                "(more exceptions exist beyond depth limit)"
                if marker == "max_depth_reached"
                else "(chain cycles back to an earlier exception)"
            )
            lines.append("... earlier chain truncated: " + marker + " " + note)
            lines.append("")
            continue
        _render_one_concise(link, lines)
        rel = link.get("relation")
        lines.append("")
        if rel == "cause":
            lines.append(
                "The above exception was the direct cause of the following exception:"
            )
        else:
            lines.append(
                "During handling of the above exception, another exception occurred:"
            )
        lines.append("")

    _render_one_concise(data, lines)

    failures = data.get("partial_failures") or []
    if failures:
        lines.append("")
        lines.append(
            "[" + str(len(failures))
            + " partial capture failure(s) inside error_handler:]"
        )
        for f in failures:
            lines.append(
                "  - " + str(f.get("step", "?")) + ": " + str(f.get("error", "?"))
            )

    return "\n".join(lines)


def _render_one_concise(d, lines):
    """Render one exception's traceback + header + type-specific + notes into
    the running `lines` list. Used for both the primary exception and each
    chain link, so the layout is consistent throughout the report."""
    tb = d.get("traceback") or []
    if tb:
        lines.append("Traceback (most recent call last):")
        for frame in tb:
            lines.append(
                '  File "' + str(frame.get("file", "?")) + '", line '
                + str(frame.get("line", "?")) + ", in "
                + str(frame.get("function", "?"))
            )
            if frame.get("code"):
                lines.append("    " + str(frame["code"]))
            locs = frame.get("locals")
            if locs:
                for k, v in locs.items():
                    lines.append("      " + str(k) + " = " + str(v))

    typ = d.get("type", "?")
    module = d.get("module", "")
    if module and module not in ("builtins", "__main__"):
        header_type = module + "." + typ
    else:
        header_type = typ
    msg = d.get("message", "")
    if msg:
        lines.append(header_type + ": " + msg)
    else:
        lines.append(header_type)

    ts = d.get("type_specific") or {}
    ts_parts = [str(k) + "=" + str(v) for k, v in ts.items() if v is not None]
    if ts_parts:
        lines.append("  [" + ", ".join(ts_parts) + "]")

    for note in d.get("notes") or []:
        lines.append("  note: " + str(note))


def _format_heavy(data):
    """Heavy / LLM-friendly edition. Fully labeled section by section, with
    every chain link rendered with its own where-it-happened block, and
    partial-failures explicitly called out so an LLM reader knows what was
    missed.

    Chain ordering here is nearest-to-oldest (the walker's natural order),
    NOT chronological. Rationale: the LLM has already seen the primary; the
    natural next question is "what's the most direct cause?", then "what's
    further back?". A structured-data view, not a narrative."""
    if data.get("error_handler_failed"):
        return (
            "=== ERROR REPORT (heavy edition) ===\n\n"
            "ERROR HANDLER FAILED\n"
            "  The error handler itself raised while trying to describe the\n"
            "  original exception. The most primitive information available:\n\n"
            "  Original exception type: "
            + str(data.get("fallback_type", "?")) + "\n"
            "  Original exception repr: "
            + str(data.get("fallback_repr", "?")) + "\n"
            "  Handler failure: "
            + str(data.get("handler_failure", "?")) + "\n\n"
            "=== END REPORT ==="
        )
    if data.get("no_active_exception"):
        return (
            "=== ERROR REPORT (heavy edition) ===\n\n"
            "NO ACTIVE EXCEPTION\n"
            "  describe_error() was called with no argument and no active\n"
            "  exception in sys.exc_info(). Nothing to describe.\n\n"
            "=== END REPORT ==="
        )

    lines = []
    lines.append("=== ERROR REPORT (heavy edition) ===")
    lines.append("")
    lines.append("PRIMARY EXCEPTION")
    _render_one_heavy(data, lines, indent="  ")

    chain = data.get("chain") or []
    lines.append("")
    if chain:
        real_links = [c for c in chain if not c.get("truncated")]
        lines.append(
            "CAUSE / CONTEXT CHAIN (" + str(len(real_links))
            + " chained exception(s); listed nearest-to-oldest as walked via "
            "__cause__ / __context__)"
        )
        for idx, link in enumerate(chain, start=1):
            lines.append("")
            if link.get("truncated"):
                marker = link["truncated"]
                explain = (
                    "(more exceptions exist beyond max_chain_depth)"
                    if marker == "max_depth_reached"
                    else "(chain cycles back to an earlier exception)"
                )
                lines.append(
                    "  --- Link " + str(idx) + ": truncated ("
                    + marker + ") " + explain + " ---"
                )
                continue
            rel = link.get("relation", "?")
            rel_phrase = {
                "cause": "explicit cause (raise ... from ...)",
                "context": "implicit context (exception raised while handling another)",
            }.get(rel, str(rel))
            lines.append("  --- Link " + str(idx) + ": " + rel_phrase + " ---")
            _render_one_heavy(link, lines, indent="    ")
    else:
        lines.append("CAUSE / CONTEXT CHAIN")
        lines.append("  (no chained exceptions)")

    lines.append("")
    failures = data.get("partial_failures") or []
    if failures:
        lines.append("INTERNAL CAPTURE ISSUES (" + str(len(failures)) + ")")
        lines.append(
            "  The error handler caught the following failures while introspecting."
        )
        lines.append(
            "  These are surprises in the exception itself (broken __repr__, etc.),"
        )
        lines.append(
            "  not problems in the original calling code. Affected fields used"
        )
        lines.append("  fallback values.")
        for f in failures:
            lines.append(
                "    - " + str(f.get("step", "?")) + ": " + str(f.get("error", "?"))
            )
    else:
        lines.append("INTERNAL CAPTURE ISSUES")
        lines.append("  None - the error handler captured everything successfully.")

    lines.append("")
    lines.append("=== END REPORT ===")
    return "\n".join(lines)


def _render_one_heavy(d, lines, indent):
    """Render one exception in heavy/labeled format into the running `lines`
    list. Used for both the primary exception and each chain link, just with
    different indent levels so chain links nest visually under their headers."""
    typ = d.get("type", "?")
    module = d.get("module", "")
    fq = (module + "." + typ) if module else typ
    lines.append(indent + "Fully-qualified type: " + fq)
    lines.append(indent + "Message: " + str(d.get("message", "")))
    lines.append(indent + "Repr: " + str(d.get("repr", "<missing>")))

    args = d.get("args") or ()
    if args:
        lines.append(indent + "Args:")
        for i, a in enumerate(args):
            lines.append(indent + "  [" + str(i) + "] " + str(a))
    else:
        lines.append(indent + "Args: (none)")

    notes = d.get("notes") or []
    if notes:
        lines.append(indent + "Notes:")
        for n in notes:
            lines.append(indent + "  - " + str(n))
    else:
        lines.append(indent + "Notes: (none)")

    extra = d.get("extra_attrs") or {}
    if extra:
        lines.append(indent + "Extra attributes:")
        for k, v in extra.items():
            lines.append(indent + "  " + str(k) + " = " + str(v))
    else:
        lines.append(indent + "Extra attributes: (none)")

    ts = d.get("type_specific") or {}
    if ts:
        lines.append(indent + "Type-specific details:")
        for k, v in ts.items():
            lines.append(indent + "  " + str(k) + ": " + str(v))
    else:
        lines.append(
            indent
            + "Type-specific details: (no extractor registered for this exception type)"
        )

    tb = d.get("traceback") or []
    if tb:
        lines.append(
            indent + "Where it happened (most recent call last, "
            + str(len(tb)) + " frame(s)):"
        )
        for i, frame in enumerate(tb, start=1):
            lines.append(indent + "  Frame " + str(i) + ":")
            lines.append(indent + "    File: " + str(frame.get("file", "?")))
            lines.append(indent + "    Line: " + str(frame.get("line", "?")))
            lines.append(indent + "    Function: " + str(frame.get("function", "?")))
            code = frame.get("code")
            if code:
                lines.append(indent + "    Code: " + str(code))
            locs = frame.get("locals")
            if locs:
                lines.append(indent + "    Locals:")
                for k, v in locs.items():
                    lines.append(indent + "      " + str(k) + " = " + str(v))
    else:
        lines.append(indent + "Where it happened: (no traceback available)")


# ---------------------------------------------------------------------------
# Smoke test: run this file directly to see the handler in action.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def deep(x):
        return shallow(x)

    def shallow(x):
        return int(x)

    try:
        deep("not a number")
    except Exception as e:
        report = describe_error(e)
        print("=" * 60)
        print("to_string():")
        print("=" * 60)
        print(report)
        print()
        print("=" * 60)
        print("to_dict():")
        print("=" * 60)
        for k, v in report.to_dict().items():
            print("  " + str(k) + ": " + repr(v))
