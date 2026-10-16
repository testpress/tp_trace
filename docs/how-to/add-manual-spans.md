# Add a manual span

Wrap the operation that needs visibility with `traced_span`.

```python
from opentelemetry.trace import SpanKind
from tp_trace.tracing import traced_span

def calculate_quote(order):
    with traced_span(
        "quote.calculate",
        kind=SpanKind.INTERNAL,
        attributes={"order.id": order.id},
    ):
        return run_quote_engine(order)
```

Exceptions inside the block are recorded on the span and then re-raised. Do not catch exceptions solely for tracing.
