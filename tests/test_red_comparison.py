"""Offline tests of the live reconciliation tool's observation boundaries."""

import importlib.util
import io
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "compare_red_metrics", Path(__file__).parent / "e2e/compare_red_metrics.py",
)
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


def test_prometheus_query_pins_time_and_rejects_api_errors(monkeypatch):
    urls = []

    def open_response(request, timeout):
        urls.append(request.full_url)
        return io.StringIO('{"status":"error","error":"invalid query"}')

    monkeypatch.setattr(comparison, "urlopen", open_response)
    with pytest.raises(RuntimeError, match="invalid query"):
        comparison.query_prometheus("http://prometheus", "sum(calls_total)", 123.5)
    assert "time=123.5" in urls[0]


@pytest.mark.parametrize("results", [[], [{"value": [0, "NaN"]}], [{"value": [0, "+Inf"]}]])
def test_missing_or_invalid_metrics_are_unavailable(results):
    assert comparison.display(comparison.metric_value(results)) == "N/A"


def test_benchmark_uses_reset_aware_queries_and_closed_window(monkeypatch, capsys):
    clock = [0.0]
    snapshots = iter([0, 110])
    queries = []

    def client(url):
        clock[0] += 1  # Include final snapshot latency in the measured interval.
        return {"stats": [{"name": "Aggregated", "num_requests": next(snapshots), "num_failures": 0}]}

    def wait(seconds):
        clock[0] += seconds

    def query(url, expr, timestamp):
        queries.append((expr, timestamp, clock[0]))
        return [{"value": [timestamp, "110"]}]

    monkeypatch.setattr(comparison, "query_locust_stats", client)
    monkeypatch.setattr(comparison, "query_prometheus", query)
    monkeypatch.setattr(comparison, "wait_for_exports", wait)
    monkeypatch.setattr(comparison.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(comparison.time, "time", lambda: 1000 + clock[0])
    comparison.run_benchmark("locust", "prometheus", 'project"name', 10, export_interval=5)

    assert "sum(increase(calls_total{" in queries[0][0]
    assert "[11000ms]" in queries[0][0]
    assert 'project_name="project\\"name"' in queries[0][0]
    assert all(timestamp == 1012 and observed == 17 for _, timestamp, observed in queries)
    output = capsys.readouterr().out
    assert "10.00 req/s" in output
    assert "APPROXIMATE BENCHMARK (11.00s" in output
    assert "Not an exact-count assertion" in output


def test_benchmark_does_not_report_missing_metrics_as_zero(monkeypatch, capsys):
    monkeypatch.setattr(comparison, "query_locust_stats", lambda url: {"stats": [{"name": "Aggregated"}]})
    monkeypatch.setattr(comparison, "query_prometheus", lambda *args: [])
    monkeypatch.setattr(comparison, "wait_for_exports", lambda seconds: None)
    comparison.run_benchmark("locust", "prometheus", "project", 120)
    assert "N/A" in capsys.readouterr().out


def test_benchmark_rejects_client_reset_and_too_short_window(monkeypatch):
    with pytest.raises(ValueError, match="two metric export intervals"):
        comparison.run_benchmark("locust", "prometheus", "project", 10)
    snapshots = iter([100, 1])
    monkeypatch.setattr(comparison, "query_locust_stats", lambda url: {
        "stats": [{"name": "Aggregated", "num_requests": next(snapshots)}],
    })
    monkeypatch.setattr(comparison, "wait_for_exports", lambda seconds: None)
    with pytest.raises(RuntimeError, match="Locust counters reset"):
        comparison.run_benchmark("locust", "prometheus", "project", 120)


def test_observations_do_not_compare_run_totals_with_window_counts(monkeypatch, capsys):
    monkeypatch.setattr(comparison, "query_locust_stats", lambda url: {
        "stats": [{"name": "Aggregated", "num_requests": 500}],
    })
    queries = []

    def query(url, expr, timestamp):
        queries.append((expr, timestamp))
        return []

    monkeypatch.setattr(comparison, "query_prometheus", query)
    comparison.run_comparison("locust", "prometheus", "project")
    output = capsys.readouterr().out
    assert "no match percentage is inferred" in output
    assert "N/A" in output
    assert len({timestamp for _, timestamp in queries}) == 1
    assert all("increase(" in expr or "rate(" in expr for expr, _ in queries)
