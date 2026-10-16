# See your first trace

In this tutorial, we will add `tp_trace` to a Django application and find one request in Grafana.

## 1. Install the SDK

```bash
pip install git+https://github.com/testpress/tp_trace.git
```

## 2. Configure the collector endpoint

Set the endpoint supplied by your environment:

```bash
export TP_TRACE_ENDPOINT=http://<collector-host>:4318
export TP_TRACE_PROJECT_NAME=my-django-app
```

## 3. Initialise tracing

Add this once to Django's `settings.py`:

```python
import tp_trace

tp_trace.init()
```

## 4. Start the application and make a request

Open any Django route in the application. The request creates a `django.request` trace with child spans for supported libraries used during that request.

## 5. View the trace

Open the team Grafana instance, choose the Tempo data source, and search for `service.name = my-django-app`. Open a recent trace to see the request waterfall.

For configuration options, see the [configuration reference](../reference/configuration.md).
