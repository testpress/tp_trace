# Add a custom integration

Create a `BaseIntegration` subclass for the library to patch.

```python
from tp_trace.integrations.base import BaseIntegration

class MyLibraryIntegration(BaseIntegration):
    name = "my-library"

    def is_installed(self):
        try:
            import my_library
            return True
        except ImportError:
            return False

    def _apply_patch(self):
        self.wrap("my_library.Client", "send", trace_send)
```

Implement `trace_send` with the wrapt wrapper signature and use `traced_span` around the original call:

```python
from opentelemetry.trace import SpanKind
from tp_trace.tracing import traced_span

def trace_send(wrapped, instance, args, kwargs):
    with traced_span("my-library.send", kind=SpanKind.CLIENT):
        return wrapped(*args, **kwargs)
```

Register the integration before `tp_trace.init()`:

```python
from tp_trace.integrations.manager import get_integration_manager

get_integration_manager().register("my-library", MyLibraryIntegration)
```
