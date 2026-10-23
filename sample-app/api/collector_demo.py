"""Bounded, opt-in workload for the local Collector regression demo."""

from django.conf import settings
from django.http import JsonResponse


MAX_OPERATIONS = 256
WORKLOAD_ROUTES = {
    "core": "api/collector-demo/<int:operation>/",
    "catalog": "api/collector-demo/catalog/<int:operation>/",
    "checkout": "api/collector-demo/checkout/<int:operation>/",
    "search": "api/collector-demo/search/<int:operation>/",
}


def collector_demo(request, operation, workload="core"):
    if not getattr(settings, "COLLECTOR_DEMO_ENABLED", False):
        return JsonResponse({"error": "Collector demo is disabled"}, status=404)
    if request.method != "GET":
        return JsonResponse({"error": "Use GET"}, status=405)
    if workload not in WORKLOAD_ROUTES:
        return JsonResponse({"error": "Unknown workload"}, status=400)
    maximum = 16384 if getattr(settings, "COLLECTOR_DEMO_SOAK", False) else MAX_OPERATIONS
    if not 0 <= operation < maximum:
        return JsonResponse({"error": "operation must be between 0 and {}".format(maximum - 1)}, status=400)

    try:
        from opentelemetry.trace import SpanKind, get_current_span
        from tp_trace.tracing import traced_span
    except ImportError:
        return JsonResponse({"error": "tp_trace is required for the demo"}, status=501)

    # A finite operation corpus deliberately exercises the incident's cardinality
    # pattern. No DB writes, external calls, or arbitrary user-supplied labels.
    route = WORKLOAD_ROUTES[workload]
    common = {"normalized.service": "collector-demo", "http.route_template": route, "http.route": route}
    with traced_span(
        "demo.client", kind=SpanKind.CLIENT,
        attributes={**common, "normalized.operation": "demo.operation.{}".format(operation)},
    ):
        for step in range(3):
            with traced_span(
                "demo.internal", kind=SpanKind.INTERNAL,
                attributes={**common, "normalized.operation": "demo.internal.{}.{}".format(operation, step)},
            ):
                pass
    trace_id = "{:032x}".format(get_current_span().get_span_context().trace_id)
    return JsonResponse({"operation": operation, "trace_id": trace_id, "internal_spans": 3})
