#!/usr/bin/env python3
"""
tiny-mcp-observability — MCP server with built-in tracing, metrics, and span recording.

Zero dependencies. Stdlib only. Records every tool call, LLM call, and span.
Exposes queries via MCP tools: list_traces, get_trace, get_metrics, export_spans.

Use as:
    python -m tiny_mcp_observability
    # or: import from your agent setup

MIT License. https://github.com/hussain-alsaibai/tiny-mcp-observability
"""

import json
import time
import uuid
import threading
import traceback
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SpanStatus(Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


class SpanKind(Enum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    WORKFLOW = "workflow"
    GATE = "gate"


@dataclass
class Span:
    trace_id: str
    span_id: str
    name: str
    kind: str
    start_ms: float
    end_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    attributes: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent_id: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.end_ms is None:
            return time.time() * 1000 - self.start_ms
        return self.end_ms - self.start_ms

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "duration_ms": round(self.duration_ms, 2),
            "kind": self.kind,
        }


@dataclass
class Metric:
    name: str
    count: int = 0
    total_ms: float = 0.0
    errors: int = 0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def record(self, duration_ms: float, tokens_in: int = 0, tokens_out: int = 0,
               cost_usd: float = 0.0, error: bool = False):
        self.count += 1
        self.total_ms += duration_ms
        self.errors += int(error)
        self.min_ms = min(self.min_ms, duration_ms)
        self.max_ms = max(self.max_ms, duration_ms)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost_usd

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "count": self.count,
            "avg_ms": round(self.avg_ms, 2),
            "min_ms": round(self.min_ms if self.min_ms != float("inf") else 0, 2),
            "max_ms": round(self.max_ms, 2),
            "error_rate": round(self.errors / self.count, 3) if self.count else 0,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "throughput_per_sec": round(1000 / self.avg_ms, 2) if self.avg_ms > 0 else 0,
        }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Store:
    """Thread-safe in-memory store for spans and metrics."""

    def __init__(self, max_traces: int = 10000):
        self._lock = threading.RLock()
        self._traces: dict[str, dict] = {}       # trace_id -> {meta, spans}
        self._spans: dict[str, Span] = {}        # span_id -> Span
        self._metrics: dict[str, Metric] = defaultdict(Metric)
        self._trace_order: list[str] = []         # FIFO for eviction
        self._max_traces = max_traces

    # --- Spans ---

    def start_span(self, name: str, kind: str = "tool",
                   trace_id: str | None = None, parent_id: str | None = None,
                   **attrs) -> str:
        span_id = uuid.uuid4().hex[:16]
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            name=name,
            kind=kind,
            start_ms=time.time() * 1000,
            attributes=attrs,
            parent_id=parent_id,
        )
        with self._lock:
            self._spans[span_id] = span
            if trace_id not in self._traces:
                self._evict_if_needed()
                self._traces[trace_id] = {"meta": {}, "spans": []}
                self._trace_order.append(trace_id)
            self._traces[trace_id]["spans"].append(span)
        return span_id

    def end_span(self, span_id: str, status: str = "ok",
                 error: str | None = None,
                 input_tokens: int | None = None,
                 output_tokens: int | None = None,
                 cost_usd: float | None = None,
                 **attrs):
        with self._lock:
            span = self._spans.get(span_id)
            if span is None:
                return
            span.end_ms = time.time() * 1000
            span.status = status
            span.error = error
            span.input_tokens = input_tokens
            span.output_tokens = output_tokens
            span.cost_usd = cost_usd
            for k, v in attrs.items():
                span.attributes[k] = v

            # Record metrics
            metric = self._metrics[span.name]
            metric.record(
                duration_ms=span.duration_ms,
                tokens_in=span.input_tokens or 0,
                tokens_out=span.output_tokens or 0,
                cost_usd=span.cost_usd or 0.0,
                error=(status == "error"),
            )
            # LLM-specific metric
            if span.kind == "llm":
                llm_metric = self._metrics["llm.total"]
                llm_metric.record(
                    duration_ms=span.duration_ms,
                    tokens_in=span.input_tokens or 0,
                    tokens_out=span.output_tokens or 0,
                    cost_usd=span.cost_usd or 0.0,
                    error=(status == "error"),
                )

    def _evict_if_needed(self):
        while len(self._traces) >= self._max_traces and self._trace_order:
            old = self._trace_order.pop(0)
            for s in self._traces.pop(old, {}).get("spans", []):
                self._spans.pop(s.span_id, None)

    # --- Queries ---

    def get_trace(self, trace_id: str) -> dict | None:
        with self._lock:
            t = self._traces.get(trace_id)
            if t is None:
                return None
            return {
                "trace_id": trace_id,
                "spans": [s.to_dict() for s in t["spans"]],
                "meta": t["meta"],
            }

    def list_traces(self, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._lock:
            trace_ids = list(reversed(self._trace_order))
            out = []
            for tid in trace_ids[offset:offset + limit]:
                spans = self._traces[tid]["spans"]
                if not spans:
                    continue
                root = next((s for s in spans if s.parent_id is None), spans[0])
                total_ms = sum(s.duration_ms for s in spans)
                total_cost = sum(s.cost_usd or 0 for s in spans)
                total_tokens = sum((s.input_tokens or 0) + (s.output_tokens or 0) for s in spans)
                errors = sum(1 for s in spans if s.status == "error")
                out.append({
                    "trace_id": tid,
                    "name": root.name,
                    "span_count": len(spans),
                    "total_ms": round(total_ms, 2),
                    "total_cost_usd": round(total_cost, 6),
                    "total_tokens": total_tokens,
                    "errors": errors,
                    "start_ms": root.start_ms,
                    "end_ms": max((s.end_ms or 0) for s in spans),
                })
            return out

    def get_metrics(self, name_prefix: str = "") -> list[dict]:
        with self._lock:
            metrics = [
                m.to_dict() for m in self._metrics.values()
                if name_prefix == "" or m.name.startswith(name_prefix)
            ]
            return sorted(metrics, key=lambda x: x["count"], reverse=True)

    def get_span(self, span_id: str) -> dict | None:
        with self._lock:
            span = self._spans.get(span_id)
            return span.to_dict() if span else None

    def export_spans(self, trace_id: str | None = None,
                     format: str = "json",
                     since_ms: float | None = None) -> str:
        """Export spans as JSON or NDJSON."""
        with self._lock:
            if trace_id:
                traces = [self._traces.get(trace_id, {})]
            else:
                cutoff = (time.time() * 1000 - since_ms) if since_ms else 0
                traces = [t for _, t in self._traces.items()
                          if not cutoff or any(s.start_ms >= cutoff for s in t["spans"])]

            all_spans = []
            for t in traces:
                all_spans.extend(t.get("spans", []))

            if format == "ndjson":
                return "\n".join(json.dumps(s.to_dict()) for s in all_spans)
            return json.dumps(all_spans, indent=2, default=str)

    def clear(self):
        with self._lock:
            self._traces.clear()
            self._spans.clear()
            self._metrics.clear()
            self._trace_order.clear()


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

class Observed:
    """Decorator for auto-instrumenting functions."""

    def __init__(self, store: Store, name: str | None = None, kind: str = "tool"):
        self.store = store
        self.name = name
        self.kind = kind

    def __call__(self, fn: Callable) -> Callable:
        import functools
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            name = self.name or fn.__name__
            span_id = self.store.start_span(name, self.kind)
            try:
                result = fn(*args, **kwargs)
                self.store.end_span(span_id, status="ok")
                return result
            except Exception as e:
                self.store.end_span(span_id, status="error", error=traceback.format_exc())
                raise
        return wrapper

    async def __call_async__(self, async_fn: Callable) -> Callable:
        import functools
        @functools.wraps(async_fn)
        async def wrapper(*args, **kwargs):
            name = self.name or async_fn.__name__
            span_id = self.store.start_span(name, self.kind)
            try:
                result = await async_fn(*args, **kwargs)
                self.store.end_span(span_id, status="ok")
                return result
            except Exception as e:
                self.store.end_span(span_id, status="error", error=traceback.format_exc())
                raise
        return wrapper


# ---------------------------------------------------------------------------
# Global store
# ---------------------------------------------------------------------------

_store = Store()


def start_span(name: str, kind: str = "tool", **attrs) -> str:
    return _store.start_span(name, kind, **attrs)


def end_span(span_id: str, **kwargs):
    _store.end_span(span_id, **kwargs)


def observe(name: str | None = None, kind: str = "tool"):
    return Observed(_store, name, kind)


def get_store() -> Store:
    return _store


# ---------------------------------------------------------------------------
# MCP Server (stdlib — JSON-RPC over stdin/stdout)
# ---------------------------------------------------------------------------

class MCPServer:
    """Minimal MCP server exposing observability tools."""

    MANIFEST = {
        "name": "tiny-mcp-observability",
        "version": "1.0.0",
        "description": "Tracing, metrics, and span recording for AI agent pipelines",
        "tools": [
            {
                "name": "start_trace",
                "description": "Start a new trace and return a trace_id. Optionally name the root span.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Name of the trace/root span"},
                        "kind": {"type": "string", "description": "Kind: agent, llm, tool, workflow, gate"},
                    },
                },
            },
            {
                "name": "start_span",
                "description": "Start a child span within an existing trace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string", "description": "Parent trace ID"},
                        "parent_id": {"type": "string", "description": "Parent span ID (omit for root)"},
                        "name": {"type": "string", "description": "Span name"},
                        "kind": {"type": "string", "description": "Span kind: agent, llm, tool, workflow, gate"},
                    },
                },
            },
            {
                "name": "end_span",
                "description": "End a running span and record its outcome.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "span_id": {"type": "string", "description": "The span to close"},
                        "status": {"type": "string", "description": "ok | error | timeout"},
                        "error": {"type": "string", "description": "Error message if status=error"},
                        "input_tokens": {"type": "integer"},
                        "output_tokens": {"type": "integer"},
                        "cost_usd": {"type": "number"},
                    },
                },
            },
            {
                "name": "list_traces",
                "description": "List recent traces with summary stats.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 50},
                        "offset": {"type": "integer", "default": 0},
                    },
                },
            },
            {
                "name": "get_trace",
                "description": "Get full span tree for a specific trace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                    },
                    "required": ["trace_id"],
                },
            },
            {
                "name": "get_metrics",
                "description": "Get aggregated metrics per span name.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name_prefix": {"type": "string", "description": "Filter metrics by name prefix (e.g. 'llm.')"},
                    },
                },
            },
            {
                "name": "export_spans",
                "description": "Export spans as JSON or NDJSON.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                        "format": {"type": "string", "enum": ["json", "ndjson"], "default": "json"},
                        "since_ms": {"type": "number", "description": "Export spans newer than N ms ago"},
                    },
                },
            },
            {
                "name": "clear",
                "description": "Clear all stored traces and metrics.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    }

    def __init__(self, store: Store | None = None):
        self.store = store or _store
        self._request_id = 0

    def _rpc_response(self, id: int, result: Any) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": id, "result": result})

    def _rpc_error(self, id: int, code: int, message: str) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": id,
                          "error": {"code": code, "message": message}})

    def _handle_request(self, method: str, params: dict, req_id: int) -> str:
        try:
            if method == "initialize":
                return self._rpc_response(req_id, {"protocolVersion": "2024-11-05", "capabilities": {}})

            if method == "tools/list":
                return self._rpc_response(req_id, {"tools": self.MANIFEST["tools"]})

            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                result = self._call_tool(tool_name, arguments)
                return self._rpc_response(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=str)}]})

            if method == "ping":
                return self._rpc_response(req_id, {"pong": True})

            return self._rpc_error(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            return self._rpc_error(req_id, -32603, str(e))

    def _call_tool(self, name: str, args: dict) -> Any:
        if name == "start_trace":
            tid = self.store.start_span(args.get("name", "root"), kind=args.get("kind", "agent"))
            return {"trace_id": tid}

        if name == "start_span":
            tid = args.get("trace_id")
            pid = args.get("parent_id")
            sid = self.store.start_span(args["name"], kind=args.get("kind", "tool"),
                                        trace_id=tid, parent_id=pid)
            return {"span_id": sid, "trace_id": tid}

        if name == "end_span":
            self.store.end_span(
                args["span_id"],
                status=args.get("status", "ok"),
                error=args.get("error"),
                input_tokens=args.get("input_tokens"),
                output_tokens=args.get("output_tokens"),
                cost_usd=args.get("cost_usd"),
            )
            return {"ok": True}

        if name == "list_traces":
            return {"traces": self.store.list_traces(
                limit=args.get("limit", 50), offset=args.get("offset", 0))}

        if name == "get_trace":
            t = self.store.get_trace(args["trace_id"])
            if t is None:
                return {"error": "Trace not found"}
            return t

        if name == "get_metrics":
            return {"metrics": self.store.get_metrics(name_prefix=args.get("name_prefix", ""))}

        if name == "export_spans":
            return {"export": self.store.export_spans(
                trace_id=args.get("trace_id"),
                format=args.get("format", "json"),
                since_ms=args.get("since_ms"),
            )}

        if name == "clear":
            self.store.clear()
            return {"ok": True, "message": "All traces and metrics cleared"}

        return {"error": f"Unknown tool: {name}"}

    def run(self):
        """Run the MCP server over stdio."""
        import sys
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                method = req.get("method", "")
                params = req.get("params", {})
                req_id = req.get("id", 0)
                resp = self._handle_request(method, params, req_id)
                if resp:
                    print(resp, flush=True)
            except json.JSONDecodeError:
                pass


if __name__ == "__main__":
    MCPServer().run()
