"""Celery application and task definitions for the sample app."""

import os
import time
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("sample_app")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

_demo_sleep = float(os.environ.get("CELERY_DEMO_SLEEP", "0.5"))


@app.task(name="sample_app.tasks.demo_task")
def demo_task(value: int = 1, fail: bool = False) -> int:
    """Demo Celery task: returns value * 2, sleeps CELERY_DEMO_SLEEP, raises on fail=True."""
    if _demo_sleep > 0:
        time.sleep(_demo_sleep)
    if fail:
        raise ValueError(f"Demo task forced failure (value={value})")
    return value * 2


# Initialize telemetry for worker process after task registration
from config.otel import setup_telemetry  # noqa: E402

setup_telemetry()
