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


@app.task(name="sample_app.tasks.order_fulfillment_task", bind=True)
def order_fulfillment_task(self, order_id: int):
    """Asynchronous order fulfillment workflow with DB, HTTP, and Redis operations."""
    from django.core.cache import cache
    from api.models import AuditLog, Order
    import requests

    if _demo_sleep > 0:
        time.sleep(_demo_sleep)

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return {"status": "not_found", "order_id": order_id}

    # 1. External shipping label generation
    shipping_url = os.environ.get("EXTERNAL_API_URL", "http://localhost:8080/get")
    try:
        requests.get(f"{shipping_url}?shipment_order={order_id}", timeout=3)
    except Exception:
        pass

    # 2. Update order status
    order.status = "completed"
    order.notes = f"{order.notes} [Fulfilled asynchronously by Celery task {self.request.id}]".strip()
    order.save()

    # 3. Write audit log
    AuditLog.objects.create(
        action="order.fulfilled",
        entity_type="Order",
        entity_id=str(order_id),
        details=f"Order #{order_id} marked as completed by Celery worker",
    )

    # 4. Cache in Redis
    cache.set(f"order:fulfilled:{order_id}", {"order_id": order_id, "status": "completed", "time": time.time()}, timeout=300)

    return {"status": "completed", "order_id": order_id, "task_id": self.request.id}


@app.task(name="sample_app.tasks.generate_sales_report_task")
def generate_sales_report_task(title: str = "Daily Summary", fail: bool = False):
    """Asynchronous analytics report generation that writes CSV to S3 storage."""
    from django.core.cache import cache
    from django.db.models import Count, Sum
    from api.models import Order

    if _demo_sleep > 0:
        time.sleep(_demo_sleep)

    if fail:
        raise RuntimeError(f"Forced report generation failure for '{title}'")

    summary = Order.objects.aggregate(
        total_orders=Count("id"),
        total_revenue=Sum("total_amount"),
    )

    csv_data = f"Report: {title}\nTimestamp: {time.time()}\nTotal Orders: {summary.get('total_orders')}\nTotal Revenue: {summary.get('total_revenue')}\n"

    # Attempt S3 upload
    s3_uploaded = False
    try:
        import botocore.session
        endpoint_url = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
        session = botocore.session.get_session()
        client = session.create_client(
            "s3",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "mock_access_key"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "mock_secret_key"),
            endpoint_url=endpoint_url,
        )
        bucket = "sample-products-bucket"
        try:
            client.create_bucket(Bucket=bucket)
        except Exception:
            pass
        key = f"reports/sales_report_{int(time.time())}.csv"
        client.put_object(Bucket=bucket, Key=key, Body=csv_data.encode("utf-8"))
        s3_uploaded = True
    except Exception:
        pass

    cache.set("reports:latest_sales_summary", summary, timeout=300)

    return {
        "status": "generated",
        "title": title,
        "summary": summary,
        "s3_uploaded": s3_uploaded,
    }


@app.task(name="sample_app.tasks.flaky_retry_task", bind=True, max_retries=3)
def flaky_retry_task(self, fail_count: int = 2):
    """Task designed to fail and retry before succeeding, demonstrating Celery retry spans."""
    attempt = self.request.retries
    if attempt < fail_count:
        raise self.retry(exc=RuntimeError(f"Flaky task transient failure (attempt {attempt + 1}/{fail_count})"), countdown=0.5)

    return {"status": "succeeded", "retries_taken": attempt}


# Initialize telemetry for worker process after task registration
from config.otel import setup_telemetry  # noqa: E402

setup_telemetry()
