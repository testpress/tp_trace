"""Compare native RED and spanmetrics export cost without sending live traffic.

Run once per source tree using --source-root; use --rate 0 for a queue-pressure
burst. Payload bytes are uncompressed OTLP protobuf, excluding HTTP/TLS overhead.
This microbenchmark is not a production capacity guarantee.
"""

import argparse
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--spans", type=int, default=10000)
    parser.add_argument("--rate", type=float, default=2000)
    parser.add_argument("--export-delay-ms", type=float, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root / "src"))

    import tp_trace
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.trace import SpanKind

    class MeasuringExporter(SpanExporter):
        def __init__(self):
            self.names = set()
            self.payload_bytes = 0

        def export(self, spans):
            self.payload_bytes += len(encode_spans(spans).SerializeToString())
            self.names.update(span.name for span in spans)
            time.sleep(args.export_delay_ms / 1000)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

    reader = InMemoryMetricReader()
    exporter = MeasuringExporter()
    provider = tp_trace.init(
        project="export-benchmark", sample_rate=0.01, auto_patch=False,
        exporter=exporter, metric_readers=[reader], export_batch=True,
    )
    tracer = provider.get_tracer("benchmark")
    sampled = set()
    started = time.perf_counter()
    cpu = time.process_time()
    for i in range(args.spans):
        if args.rate > 0:
            delay = started + i / args.rate - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        with tracer.start_as_current_span(str(i), kind=SpanKind.SERVER, attributes={
            "http.method": "GET", "http.route_template": "/benchmark/<id>/",
            "http.status_code": 200, "normalized.service": "django",
        }) as span:
            if span.get_span_context().trace_flags.sampled:
                sampled.add(span.name)
    generated_seconds = time.perf_counter() - started
    assert provider.force_flush()
    data = reader.get_metrics_data()
    metric_bytes = len(encode_metrics(data).SerializeToString()) if data else 0
    result = {
        "source": str(args.source_root), "spans": args.spans, "sample_rate": 0.01,
        "rate": args.rate, "export_delay_ms": args.export_delay_ms,
        "cpu_seconds": round(time.process_time() - cpu, 4),
        "generation_seconds": round(generated_seconds, 4),
        "exported_spans": len(exporter.names), "selected_spans": len(sampled),
        "selected_spans_lost": len(sampled - exporter.names),
        "non_exported_spans": args.spans - len(exporter.names),
        "trace_payload_bytes": exporter.payload_bytes, "metric_payload_bytes": metric_bytes,
    }
    provider.shutdown()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
