# tiny-mcp-observability

> **Tracing, metrics, and span recording for AI agent pipelines — zero dependencies, one file.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://github.com/hussain-alsaibai/tiny-mcp-observability#install)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](tiny_mcp_observability.py)
[![CI](https://github.com/hussain-alsaibai/tiny-mcp-observability/actions/workflows/ci.yml/badge.svg)](https://github.com/hussain-alsaibai/tiny-mcp-observability/actions)

Every production AI agent needs to answer: *what happened, how long did it take, what did it cost, and where did it fail?*

`tiny-mcp-observability` is an **MCP server** that instruments your agent pipeline — every tool call, LLM call, and span is recorded. Query it via MCP tools: `list_traces`, `get_trace`, `get_metrics`, `export_spans`.

Zero dependencies. Works as a standalone MCP server or as an inline Python library.

## Why?

- **LangSmith** — excellent, but $$$. Needs account setup.
- **`otel` packages** — powerful, but 6+ deps, complex SDK.
- **Manual logging** — works, but can't query it like a database.

`tiny-mcp-observability` is the **zero-config middle path**: instruments everything, stores it in memory, exposes it via MCP. You can ship it today and ask "what failed in the last 1000 traces?" in under a second.

## Features

| Feature | Description |
|---------|-------------|
| **Span trees** | Hierarchical spans — agent → LLM → tool → sub-tool |
| **Trace recording** | Every trace is stored, queryable, and exportable |
| **Metrics aggregation** | Per-name stats: count, avg/min/max latency, error rate, tokens, cost |
| **MCP tools** | `start_trace`, `start_span`, `end_span`, `list_traces`, `get_trace`, `get_metrics`, `export_spans`, `clear` |
| **Decorator API** | `@observe("my_func")` auto-instruments any function |
| **LLM-aware** | Records input/output tokens and USD cost per LLM call |
| **Zero dependencies** | Stdlib only — `json`, `threading`, `time`, `uuid`, `dataclasses` |
| **Eviction** | LRU eviction at 10,000 traces — never runs out of memory |
| **Export** | JSON or NDJSON export for any time window |

## Install

```bash
# As MCP server
python -m tiny_mcp_observability

# Or copy the file — it's one Python file, no dependencies:
curl -O https://raw.githubusercontent.com/hussain-alsaibai/tiny-mcp-observability/main/tiny_mcp_observability.py
```

## Quick Start

### As an MCP Server

```bash
python -m tiny_mcp_observability
# Then point any MCP client (OpenClaw, Claude SDK, Goose) at it.
```

### As a Python Library

```python
from tiny_mcp_observability import start_span, end_span, observe, get_store

# Manual spans
trace_id = start_span("agent.run", kind="agent")
llm_span = start_span("llm.call", kind="llm", trace_id=trace_id)
# ... LLM call happens ...
end_span(llm_span, status="ok", input_tokens=820, output_tokens=240, cost_usd=0.008)
end_span(trace_id, status="ok")

# Or with the decorator
@observe("process_file")
def process_file(path: str) -> dict:
    # This function is auto-instrumented
    return {"path": path, "size": 42}

# Query results
store = get_store()
traces = store.list_traces(limit=10)
metrics = store.get_metrics()
```

### Start a Trace with MCP (JSON-RPC)

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
  "name": "start_trace",
  "arguments": {"name": "bounty-hunt", "kind": "agent"}
}}
// Response: {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"trace_id\": \"a1b2c3d4...\"}"}]}}
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `start_trace` | Start a new trace. Returns a `trace_id`. |
| `start_span` | Start a child span within a trace. Returns `span_id`. |
| `end_span` | End a span. Record status, tokens, cost. |
| `list_traces` | List recent traces with summary stats (count, cost, errors). |
| `get_trace` | Get the full span tree for one trace. |
| `get_metrics` | Get aggregated per-name metrics. |
| `export_spans` | Export spans as JSON or NDJSON. |
| `clear` | Wipe all stored traces and metrics. |

## Example: Tracing a Bounty Hunt

```python
from tiny_mcp_observability import start_span, end_span

# Start the agent run
trace_id = start_span("bounty_hunt", kind="agent", repo="hussain-alsaibai/snapdb")

# Scan phase
scan = start_span("scan_issues", kind="tool", trace_id=trace_id)
# ... scanning logic ...
end_span(scan, status="ok")

# Score phase
score = start_span("score_issue", kind="tool", trace_id=trace_id)
# ... scoring logic ...
end_span(score, status="ok")

# LLM call
llm = start_span("llm.implement", kind="llm", trace_id=trace_id)
# ... LLM call with tokens ...
end_span(llm, status="ok", input_tokens=1200, output_tokens=800, cost_usd=0.024)

# PR creation
pr = start_span("create_pr", kind="tool", trace_id=trace_id)
# ... git operations ...
end_span(pr, status="ok")

end_span(trace_id, status="ok")
```

Query the trace:

```python
store = get_store()
trace = store.get_trace(trace_id)
print(f"Total cost: ${sum(s.cost_usd or 0 for s in trace['spans']):.4f}")
print(f"Total time: {max(s.end_ms or 0 for s in trace['spans']) - trace['spans'][0].start_ms:.1f}ms")
```

## Output Example

```json
{
  "trace_id": "a1b2c3d4e5f6",
  "spans": [
    {
      "name": "bounty_hunt",
      "kind": "agent",
      "duration_ms": 4821.3,
      "status": "ok",
      "children": [
        {"name": "scan_issues", "kind": "tool", "duration_ms": 234.1, "status": "ok"},
        {"name": "llm.implement", "kind": "llm", "duration_ms": 3800.2, "status": "ok",
         "input_tokens": 1200, "output_tokens": 800, "cost_usd": 0.024},
        {"name": "create_pr", "kind": "tool", "duration_ms": 782.0, "status": "ok"}
      ]
    }
  ]
}
```

## Metrics Example

```
$ python -m tiny_mcp_observability
> tools/call → get_metrics

{
  "metrics": [
    {"name": "llm.call", "count": 847, "avg_ms": 1823.4, "min_ms": 234, "max_ms": 12800,
     "error_rate": 0.034, "tokens_in": 482000, "tokens_out": 218000, "cost_usd": 18.42},
    {"name": "github.list_issues", "count": 1204, "avg_ms": 89.2, "min_ms": 45, "max_ms": 320,
     "error_rate": 0.008, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0},
    {"name": "github.create_pr", "count": 3, "avg_ms": 1102.0, "min_ms": 980, "max_ms": 1240,
     "error_rate": 0.0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0}
  ]
}
```

## Architecture

```
┌─────────────────────────────────────────────┐
│  Your Agent Code                            │
│                                             │
│  @observe("llm.call")                      │
│  def llm_call(...):                         │
│                                             │
│    start_span("my_span")                    │
│    ...                                      │
│    end_span(span_id, ...)                   │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  tiny_mcp_observability (in-process)         │
│                                             │
│  Store: in-memory, thread-safe              │
│  ┌──────────────────────────────────────┐   │
│  │ traces: {trace_id → [spans]}          │   │
│  │ metrics: {name → Metric}              │   │
│  │ spans: {span_id → Span}               │   │
│  └──────────────────────────────────────┘   │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  MCP Protocol (stdio JSON-RPC)              │
│                                             │
│  list_traces, get_trace, get_metrics,        │
│  export_spans, start_span, end_span         │
└─────────────────────────────────────────────┘
```

## Ecosystem

Part of the **tiny-\* ecosystem** — zero-dependency, single-file Python libraries for AI agents and autonomous workflows:

- [tiny-agent](https://github.com/hussain-alsaibai/tiny-agent) — Agent runtime with ReAct loop
- [tiny-llm](https://github.com/hussain-alsaibai/tiny-llm) — Unified LLM client
- [tiny-repl](https://github.com/hussain-alsaibai/tiny-repl) — Agent pipeline debugging REPL
- [tiny-workflow](https://github.com/hussain-alsaibai/tiny-workflow) — DAG orchestration
- [tiny-trace](https://github.com/hussain-alsaibai/tiny-trace) — Distributed context propagation
- [tiny-eval](https://github.com/hussain-alsaibai/tiny-eval) — Agent eval harness
- [fast-cache](https://github.com/hussain-alsaibai/fast-cache) — LRU + TTL cache
- [tiny-log](https://github.com/hussain-alsaibai/tiny-log) — Structured logging
- [tiny-validator](https://github.com/hussain-alsaibai/tiny-validator) — Schema validation

---

MIT License © 2026 Hussain Al-Saibai
