import multiprocessing
import os

from tp_trace.integrations.gunicorn import TPTraceGunicornLogger

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = 120
keepalive = 5
reload = True
accesslog = "-"
errorlog = "-"
loglevel = "info"

logger_class = TPTraceGunicornLogger
