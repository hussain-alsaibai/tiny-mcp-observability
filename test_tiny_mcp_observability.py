"""Tests for tiny-mcp-observability.

This module shipped with no test file at all: the workflow ran
`pytest tiny_mcp_observability.py`, which collected 0 items and exited 5
("no tests collected"). The CI badge was red for a reason that had nothing
to do with the module working.
"""
import asyncio
import json
import time

import pytest

from tiny_mcp_observability import (
    Metric,
    Observed,
    Span,
    SpanKind,
    SpanStatus,
    Store,
    get_store,
    observe,
)


@pytest.fixture()
def store():
    return Store()


class TestSpan:
    def test_duration_while_open(self):
        s = Span(trace_id="t", span_id="s", name="n", kind="tool", start_ms=time.time() * 1000)
        assert s.duration_ms > 0

    def test_duration_when_closed(self):
        s = Span(trace_id="t", span_id="s", name="n", kind="tool", start_ms=1000.0, end_ms=1250.0)
        assert s.duration_ms == 250.0

    def test_to_dict_includes_kind_and_duration(self):
        s = Span(trace_id="t", span_id="s", name="n", kind="llm", start_ms=1000.0, end_ms=1010.0)
        d = s.to_dict()
        assert d["kind"] == "llm"
        assert d["duration_ms"] == 10.0
        assert d["name"] == "n"


class TestMetric:
    def test_record_updates_counters(self):
        m = Metric(name="tool")
        m.record(10.0, tokens_in=5, tokens_out=7, cost_usd=0.5)
        assert m.count == 1
        assert m.total_ms == 10.0
        assert m.tokens_in == 5
        assert m.tokens_out == 7
        assert m.cost_usd == 0.5

    def test_error_counted(self):
        m = Metric(name="tool")
        m.record(10.0, error=True)
        assert m.errors == 1

    def test_avg_zero_when_empty(self):
        assert Metric(name="tool").avg_ms == 0.0

    def test_min_max_tracked(self):
        m = Metric(name="tool")
        m.record(10.0)
        m.record(30.0)
        m.record(20.0)
        assert m.min_ms == 10.0
        assert m.max_ms == 30.0
        assert m.avg_ms == 20.0

    def test_to_dict_shape(self):
        m = Metric(name="tool")
        m.record(10.0)
        d = m.to_dict()
        assert d["name"] == "tool"
        assert d["count"] == 1
        assert d["error_rate"] == 0
        assert "throughput_per_sec" in d

    def test_to_dict_min_is_zero_not_inf_when_empty(self):
        d = Metric(name="tool").to_dict()
        assert d["min_ms"] == 0


class TestStore:
    def test_start_and_end_span(self, store):
        sid = store.start_span("op", kind="tool")
        assert sid
        store.end_span(sid, status="ok")
        span = store.get_span(sid)
        assert span["status"] == "ok"
        assert span["end_ms"] is not None

    def test_end_span_records_tokens_and_cost(self, store):
        sid = store.start_span("op")
        store.end_span(sid, input_tokens=10, output_tokens=20, cost_usd=0.25)
        span = store.get_span(sid)
        assert span["input_tokens"] == 10
        assert span["output_tokens"] == 20
        assert span["cost_usd"] == 0.25

    def test_end_unknown_span_is_noop(self, store):
        store.end_span("does-not-exist")
        assert store.get_span("does-not-exist") is None

    def test_child_span_shares_trace(self, store):
        parent = store.start_span("root", trace_id="trace-1")
        child = store.start_span("child", trace_id="trace-1", parent_id=parent)
        trace = store.get_trace("trace-1")
        assert trace is not None
        assert len(trace["spans"]) == 2
        assert store.get_span(child)["parent_id"] == parent

    def test_get_trace_missing(self, store):
        assert store.get_trace("nope") is None

    def test_metrics_recorded_per_span_name(self, store):
        sid = store.start_span("mytool")
        store.end_span(sid)
        names = [m["name"] for m in store.get_metrics()]
        assert "mytool" in names

    def test_llm_span_records_aggregate_metric(self, store):
        sid = store.start_span("call", kind="llm")
        store.end_span(sid, input_tokens=3, output_tokens=4)
        names = [m["name"] for m in store.get_metrics()]
        assert "llm.total" in names

    def test_get_metrics_prefix_filter(self, store):
        sid = store.start_span("alpha")
        store.end_span(sid)
        assert store.get_metrics("alp")
        assert store.get_metrics("zzz") == []

    def test_error_span_counts_as_error(self, store):
        sid = store.start_span("boom")
        store.end_span(sid, status="error", error="kaboom")
        traces = store.list_traces()
        assert traces[0]["errors"] == 1

    def test_list_traces_summarizes(self, store):
        sid = store.start_span("root")
        store.end_span(sid, input_tokens=2, output_tokens=3, cost_usd=0.1)
        t = store.list_traces()[0]
        assert t["span_count"] == 1
        assert t["total_tokens"] == 5
        assert t["total_cost_usd"] == 0.1

    def test_list_traces_offset(self, store):
        for i in range(3):
            sid = store.start_span("s%d" % i)
            store.end_span(sid)
        assert len(store.list_traces(limit=2)) == 2
        assert len(store.list_traces(limit=10, offset=2)) == 1

    def test_export_spans_json(self, store):
        sid = store.start_span("op")
        store.end_span(sid)
        data = json.loads(store.export_spans())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "op"

    def test_export_spans_ndjson(self, store):
        sid = store.start_span("op")
        store.end_span(sid)
        lines = [l for l in store.export_spans(format="ndjson").splitlines() if l]
        assert len(lines) == 1
        assert json.loads(lines[0])["name"] == "op"

    def test_export_spans_by_trace_id(self, store):
        sid = store.start_span("op", trace_id="t1")
        store.end_span(sid)
        data = json.loads(store.export_spans(trace_id="t1"))
        assert len(data) == 1

    def test_clear_empties_store(self, store):
        sid = store.start_span("op")
        store.end_span(sid)
        store.clear()
        assert store.get_metrics() == []
        assert store.list_traces() == []

    def test_max_traces_eviction(self):
        s = Store(max_traces=2)
        for i in range(4):
            sid = s.start_span("s%d" % i)
            s.end_span(sid)
        assert len(s.list_traces(limit=100)) <= 2

    def test_store_lock_is_reentrant(self, store):
        with store._lock:
            with store._lock:
                pass

    def test_concurrent_span_starts(self, store):
        from concurrent.futures import ThreadPoolExecutor

        def _work(i):
            sid = store.start_span("s%d" % i)
            store.end_span(sid)
            return sid

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(_work, range(50)))
        assert len(set(ids)) == 50
        assert len(store.get_metrics()) >= 1


class TestObserved:
    def test_decorator_records_success(self, store):
        @Observed(store, name="add")
        def add(a, b):
            return a + b

        assert add(1, 2) == 3
        metrics = store.get_metrics()
        assert any(m["name"] == "add" and m["count"] == 1 for m in metrics)

    def test_decorator_records_error_and_reraises(self, store):
        @Observed(store, name="boom")
        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            boom()
        metrics = store.get_metrics()
        assert any(m["name"] == "boom" and m["error_rate"] == 1.0 for m in metrics)

    def test_decorator_preserves_metadata(self, store):
        @Observed(store)
        def documented():
            """docstring"""
            return 1

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "docstring"

    def test_async_decorator(self, store):
        @Observed(store, name="afn")
        async def afn():
            await asyncio.sleep(0)
            return 7

        assert asyncio.run(afn()) == 7
        assert any(m["name"] == "afn" for m in store.get_metrics())

    def test_async_decorator_records_error(self, store):
        @Observed(store, name="aerr")
        async def aerr():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            asyncio.run(aerr())
        assert any(m["name"] == "aerr" and m["error_rate"] == 1.0 for m in store.get_metrics())


class TestModuleApi:
    def test_get_store_is_singleton(self):
        assert get_store() is get_store()

    def test_observe_returns_observed(self):
        assert isinstance(observe("x"), Observed)

    def test_enums_have_expected_values(self):
        assert SpanStatus.OK.value == "ok"
        assert SpanStatus.ERROR.value == "error"
        assert SpanStatus.TIMEOUT.value == "timeout"
        assert SpanKind.LLM.value == "llm"
        assert SpanKind.TOOL.value == "tool"
