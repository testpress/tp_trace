"""Memory observations distinguish missing data from zero and RSS from heap."""

import importlib.util
from pathlib import Path


def load_soak():
    path = Path(__file__).resolve().parent / "e2e/collector_soak.py"
    spec = importlib.util.spec_from_file_location("collector_soak_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_metrics_accept_both_collector_naming_styles():
    soak = load_soak()
    text = '# HELP otelcol_process_memory_rss RSS\notelcol_process_memory_rss{version="old"} 1024\n'
    assert soak.metric_value(text, "otelcol_process_memory_rss") == 1024
    assert soak.metric_value('otelcol_process_memory_rss_bytes 2048\n', 'otelcol_process_memory_rss') == 2048
    assert soak.metric_value(text, "otelcol_process_runtime_heap_alloc_bytes") is None


def test_refused_attempts_sum_receivers_and_preserve_zero():
    soak = load_soak()
    text = 'otelcol_receiver_refused_spans_total{receiver="a"} 0\notelcol_receiver_refused_spans_total{receiver="b"} 3\n'
    assert soak.metric_value(text, "otelcol_receiver_refused_spans", total=True) == 3
    assert soak.metric_value('otelcol_receiver_refused_spans 0\n', 'otelcol_receiver_refused_spans', total=True) == 0
    assert soak.metric_value('', 'otelcol_receiver_refused_spans', total=True) is None


def test_metric_prefix_does_not_include_created_timestamp():
    soak = load_soak()
    text = 'otelcol_receiver_refused_spans_created 123456789\notelcol_receiver_refused_spans_total 3\n'
    assert soak.metric_value(text, 'otelcol_receiver_refused_spans', total=True) == 3


def test_cpu_usage_is_counter_delta_over_elapsed_time():
    soak = load_soak()
    previous = {"cpu_seconds": 3.0, "cpu_sample_time": 10.0}
    current = {"cpu_seconds": 3.5, "cpu_sample_time": 12.0}
    assert soak.cpu_usage(previous, current) == 25.0
    assert soak.cpu_usage(current, {"cpu_seconds": 3.5, "cpu_sample_time": 14.0}) == 0


def test_cpu_usage_has_no_fabricated_first_or_reset_value():
    soak = load_soak()
    current = {"cpu_seconds": 3.0, "cpu_sample_time": 10.0}
    assert soak.cpu_usage(None, current) is None
    assert soak.cpu_usage(current, current) is None
    assert soak.cpu_usage(current, {"cpu_seconds": 1.0, "cpu_sample_time": 12.0}) is None
    assert soak.cpu_usage(current, {"cpu_seconds": None, "cpu_sample_time": 12.0}) is None


def test_cpu_counter_names_match_old_and_new_versions():
    soak = load_soak()
    for suffix in ("", "_total"):
        assert soak.metric_value('otelcol_process_cpu_seconds' + suffix + ' 2.5\n',
                                 'otelcol_process_cpu_seconds') == 2.5
