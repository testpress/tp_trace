import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.http import HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import Product
from .serializers import ProductSerializer


class BurstRateThrottle(AnonRateThrottle):
    """Custom throttle limiting unauthenticated requests to 5 per minute for testing."""
    scope = "burst_test"
    rate = "5/minute"



# Buckets already confirmed to exist, per process. Bootstrapping on every
# request would add a HeadBucket span to each cycle and pollute the operation
# mix the load test is trying to measure. Stale if a bucket is deleted mid-run,
# which the per-op error path reports honestly anyway.
_VERIFIED_BUCKETS = set()


class S3StorageView(APIView):
    """API view executing one S3 / boto3 object storage operation (with graceful fallback).

    The operation is selected with ``?op=``: ``list`` (default), ``put``,
    ``get``, ``delete`` or ``head``. Calling the endpoint with no ``op``
    preserves the original head_bucket + list_objects_v2 behaviour.

    S3 failures are deliberately swallowed and returned as HTTP 200 with a
    descriptive ``status`` -- a broken MinIO should not look like a broken
    endpoint. That means callers must check the ``ok`` field rather than the
    HTTP status code; ``status: "simulated"`` additionally means botocore was
    absent, so no boto spans were emitted at all.
    """

    DEFAULT_BUCKET = "sample-products-bucket"
    DEFAULT_KEY = "inventory/products.json"
    VALID_OPS = ("list", "put", "get", "delete", "head")

    @staticmethod
    def _ensure_bucket(client, bucket_name):
        """Best-effort bucket bootstrap, once per process per bucket."""
        if bucket_name in _VERIFIED_BUCKETS:
            return
        try:
            client.head_bucket(Bucket=bucket_name)
        except Exception:
            try:
                client.create_bucket(Bucket=bucket_name)
            except Exception:
                pass
        _VERIFIED_BUCKETS.add(bucket_name)

    @staticmethod
    def _run_op(client, op, bucket_name, key):
        """Dispatch one S3 operation. Returns a representative size/int."""
        if op == "put":
            # Unique-ish body so the write is a real write, not a no-op overwrite.
            body = b'{"product":"widget","stock":10}'
            client.put_object(Bucket=bucket_name, Key=key, Body=body)
            return len(body)

        if op == "get":
            res = client.get_object(Bucket=bucket_name, Key=key)
            return int(res.get("ContentLength", 0))

        if op == "delete":
            client.delete_object(Bucket=bucket_name, Key=key)
            return 0

        if op == "head":
            res = client.head_object(Bucket=bucket_name, Key=key)
            return int(res.get("ContentLength", 0))

        # "list" (default): the original behaviour.
        res = client.list_objects_v2(Bucket=bucket_name, Prefix="inventory/")
        return len(str(res))

    def get(self, request):
        bucket_name = request.GET.get("bucket", self.DEFAULT_BUCKET)
        key = request.GET.get("key", self.DEFAULT_KEY)
        op = request.GET.get("op", "list").lower()
        if op not in self.VALID_OPS:
            op = "list"

        try:
            import os
            import botocore.session
            from botocore.exceptions import BotoCoreError, ClientError

            endpoint_url = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")

            session = botocore.session.get_session()
            client = session.create_client(
                "s3",
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "mock_access_key"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "mock_secret_key"),
                endpoint_url=endpoint_url,
            )
            status_msg = "success"
            content_length = 42

            try:
                self._ensure_bucket(client, bucket_name)
                content_length = self._run_op(client, op, bucket_name, key)
            except (BotoCoreError, ClientError, Exception) as err:
                status_msg = f"traced_s3_call ({err.__class__.__name__})"

            return Response({
                "storage": "s3",
                "op": op,
                "bucket": bucket_name,
                "key": key,
                "status": status_msg,
                "ok": status_msg == "success",
                "bytes": content_length,
            })
        except ImportError:
            return Response({
                "storage": "s3",
                "op": op,
                "bucket": bucket_name,
                "key": key,
                "status": "simulated",
                "ok": True,
                "bytes": 42,
            })
        except Exception as exc:
            return Response({
                "storage": "s3",
                "op": op,
                "bucket": bucket_name,
                "key": key,
                "status": "handled_exception",
                "ok": False,
                "message": str(exc),
            })


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer

    @action(detail=False, methods=["get"], url_path="read-slave1")
    def read_slave1(self, request):
        products = Product.objects.using("slave1").all()
        serializer = self.get_serializer(products, many=True)
        return Response({
            "database": "slave1",
            "count": len(serializer.data),
            "products": serializer.data,
        })

    @action(detail=False, methods=["get"], url_path="read-slave2")
    def read_slave2(self, request):
        products = Product.objects.using("slave2").all()
        serializer = self.get_serializer(products, many=True)
        return Response({
            "database": "slave2",
            "count": len(serializer.data),
            "products": serializer.data,
        })

    @action(detail=False, methods=["get"], url_path="read-slave3")
    def read_slave3(self, request):
        products = Product.objects.using("slave3").all()
        serializer = self.get_serializer(products, many=True)
        return Response({
            "database": "slave3",
            "count": len(serializer.data),
            "products": serializer.data,
        })

    @action(detail=False, methods=["post", "get"], url_path="write-slave1")
    def write_slave1(self, request):
        name = request.data.get("name") if request.method == "POST" else None
        name = name or f"Slave1 Item {int(time.time())}"
        price = request.data.get("price", "29.99") if request.method == "POST" else "29.99"
        stock = int(request.data.get("stock", 10)) if request.method == "POST" else 10
        description = request.data.get("description", "Direct write to slave1 database") if request.method == "POST" else "Direct write to slave1"

        product = Product.objects.using("slave1").create(
            name=name,
            price=price,
            stock=stock,
            description=description,
        )
        return Response({
            "database": "slave1",
            "action": "INSERT",
            "product": ProductSerializer(product).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post", "get"], url_path="write-slave2")
    def write_slave2(self, request):
        name = request.data.get("name") if request.method == "POST" else None
        name = name or f"Slave2 Item {int(time.time())}"
        price = request.data.get("price", "49.99") if request.method == "POST" else "49.99"
        stock = int(request.data.get("stock", 25)) if request.method == "POST" else 25
        description = request.data.get("description", "Direct write to slave2 database") if request.method == "POST" else "Direct write to slave2"

        product = Product.objects.using("slave2").create(
            name=name,
            price=price,
            stock=stock,
            description=description,
        )
        return Response({
            "database": "slave2",
            "action": "INSERT",
            "product": ProductSerializer(product).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post", "get"], url_path="write-slave3")
    def write_slave3(self, request):
        name = request.data.get("name") if request.method == "POST" else None
        name = name or f"Slave3 Item {int(time.time())}"
        price = request.data.get("price", "99.99") if request.method == "POST" else "99.99"
        stock = int(request.data.get("stock", 50)) if request.method == "POST" else 50
        description = request.data.get("description", "Direct write to slave3 database") if request.method == "POST" else "Direct write to slave3"

        product = Product.objects.using("slave3").create(
            name=name,
            price=price,
            stock=stock,
            description=description,
        )
        return Response({
            "database": "slave3",
            "action": "INSERT",
            "product": ProductSerializer(product).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post", "get"], url_path="sync-all-dbs")
    def sync_all_dbs(self, request):
        """Creates and updates records across default, slave1, slave2, and slave3 in a single request."""
        name = request.data.get("name") if request.method == "POST" else None
        name = name or f"Omni Sync Item {int(time.time())}"
        price = request.data.get("price", "79.99") if request.method == "POST" else "79.99"
        stock = int(request.data.get("stock", 100)) if request.method == "POST" else 100

        results = {}
        for db in ["default", "slave1", "slave2", "slave3"]:
            p = Product.objects.using(db).create(
                name=f"{name} [{db}]",
                price=price,
                stock=stock,
                description=f"Multi-DB synced record on {db}",
            )
            Product.objects.using(db).filter(pk=p.pk).update(stock=stock + 5)
            results[db] = {"id": p.id, "name": p.name, "stock": stock + 5}

        cache.set("sync:last_synced_item", results, timeout=120)

        return Response({
            "status": "synchronized",
            "databases_updated": ["default", "slave1", "slave2", "slave3"],
            "redis_cache_updated": True,
            "results": results,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post", "get"], url_path="multi-db-transfer")
    def multi_db_transfer(self, request):
        """Cross-database workflow: Read primary, update slave1, write audit on slave2, update Redis."""
        amount = int(request.data.get("amount", 5)) if request.method == "POST" else 5

        # 1. Read / Create on Primary
        primary_product = Product.objects.using("default").first()
        if not primary_product:
            primary_product = Product.objects.using("default").create(name="Primary Source Item", price="19.99", stock=50)

        # 2. Update Primary stock
        Product.objects.using("default").filter(pk=primary_product.pk).update(stock=max(0, primary_product.stock - amount))

        # 3. Insert or update replica 1
        slave1_product, _ = Product.objects.using("slave1").get_or_create(
            name=f"Transferred: {primary_product.name}",
            defaults={"price": primary_product.price, "stock": 0, "description": "Received from primary"}
        )
        Product.objects.using("slave1").filter(pk=slave1_product.pk).update(stock=slave1_product.stock + amount)

        # 4. Write audit entry in replica 2
        Product.objects.using("slave2").create(
            name=f"Audit Transfer {int(time.time())}",
            description=f"Transferred {amount} units from default to slave1",
            price="0.00",
            stock=amount,
        )

        # 5. Write log entry in replica 3
        Product.objects.using("slave3").create(
            name=f"Sync Log {int(time.time())}",
            description=f"Sync log for item {primary_product.pk}",
            price="0.00",
            stock=amount,
        )

        # 6. Redis cache update
        cache_key = f"transfer:last:{primary_product.pk}"
        cache.set(cache_key, {"transferred": amount, "timestamp": time.time()}, timeout=60)

        return Response({
            "status": "transfer_complete",
            "amount": amount,
            "primary_id": primary_product.pk,
            "slave1_id": slave1_product.pk,
            "audit_slave2": True,
            "log_slave3": True,
            "redis_cached": True,
        })

    @action(detail=False, methods=["get"], url_path="cache")
    def cache_endpoint(self, request):
        cache_key = "products:all_cached"
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response({"cache_hit": True, "source": "redis_cache", "products": cached_data})

        products = Product.objects.using("default").all()
        serializer = self.get_serializer(products, many=True)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response({"cache_hit": False, "source": "database", "products": serializer.data})

    @action(detail=False, methods=["get"], url_path="redis-slow")
    def redis_slow(self, request):
        delay = float(request.query_params.get("delay", 1.5))
        redis_delay = max(0.01, delay * 0.75)
        app_delay = max(0.005, delay * 0.25)
        try:
            from django_redis import get_redis_connection
            r = get_redis_connection("default")
            lua_delay = """
            local t0 = redis.call('TIME')
            local start = t0[1] + t0[2]/1000000
            while true do
                local t1 = redis.call('TIME')
                local now = t1[1] + t1[2]/1000000
                if now - start >= tonumber(ARGV[1]) then break end
            end
            return 1
            """
            r.eval(lua_delay, 0, redis_delay)
            cache.set("slow_key", "slow_val", timeout=60)
        except Exception:
            time.sleep(redis_delay)

        t_end = time.time() + app_delay
        dummy_calc = 0
        while time.time() < t_end:
            dummy_calc += sum(i * i for i in range(100))

        return Response({"status": "slow_redis_complete", "delay": delay})

    @action(detail=False, methods=["get"], url_path="postgres-slow")
    def postgres_slow(self, request):
        delay = float(request.query_params.get("delay", 1.5))
        db = request.query_params.get("db", "default")
        db_delay = max(0.01, delay * 0.75)
        app_delay = max(0.005, delay * 0.25)

        from django.db import connection, connections
        target_conn = connections[db] if db in connections else connection
        with target_conn.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(%s)", [db_delay])

        t_end = time.time() + app_delay
        dummy_calc = 0
        while time.time() < t_end:
            dummy_calc += sum(i * i for i in range(100))

        try:
            products = Product.objects.using(db if db in connections else "default").all()[:20]
            serializer = self.get_serializer(products, many=True)
            prod_data = serializer.data
        except Exception:
            prod_data = []
        return Response({"status": "slow_postgres_complete", "delay": delay, "db_delay": db_delay, "app_delay": app_delay, "database": db, "products": prod_data})

    @action(detail=False, methods=["get"], url_path="external")
    def external_endpoint(self, request):
        from django.conf import settings
        target_url = request.query_params.get("url", getattr(settings, "EXTERNAL_API_URL", "https://github.com/"))
        timeout = int(request.query_params.get("timeout", 5))
        try:
            resp = requests.get(target_url, timeout=timeout)
            return Response({
                "status_code": resp.status_code,
                "target_url": target_url,
                "response_time_ms": resp.elapsed.total_seconds() * 1000,
                "data": resp.text[:500],
            })
        except requests.RequestException as e:
            return Response({"error": str(e), "target_url": target_url}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=False, methods=["get"], url_path="error")
    def error_endpoint(self, request):
        raise RuntimeError("Deliberate server error for OTel testing")

    @action(detail=False, methods=["get"], url_path="throttled", throttle_classes=[BurstRateThrottle])
    def throttled_endpoint(self, request):
        return Response({
            "message": "Request allowed (within 5 req/minute rate limit)",
            "status": "allowed",
            "client_ip": request.META.get("REMOTE_ADDR"),
        })

    @action(detail=False, methods=["get"], url_path="slow")
    def slow_endpoint(self, request):
        time.sleep(2)
        products = Product.objects.using("default").all()[:5]
        serializer = self.get_serializer(products, many=True)
        return Response({"message": "Slow endpoint response (2s delay)", "products": serializer.data})

    @action(detail=True, methods=["get"])
    def cached(self, request, pk=None):
        cache_key = f"product:{pk}"

        cached_product = cache.get(cache_key)
        if cached_product is not None:
            return Response({"cache_hit": True, "source": "cache", "product": cached_product})

        try:
            product = self.get_object()
        except Product.DoesNotExist:
            return Response(
                {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = self.get_serializer(product)
        cache.set(cache_key, serializer.data, timeout=300)

        return Response({"cache_hit": False, "source": "database", "product": serializer.data})

    @action(detail=True, methods=["get"])
    def slow(self, request, pk=None):
        time.sleep(2)
        try:
            product = self.get_object()
        except Product.DoesNotExist:
            return Response(
                {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
            )
        serializer = self.get_serializer(product)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def error(self, request, pk=None):
        raise RuntimeError("Deliberate server error for OTel testing")

    @action(detail=False, methods=["get"])
    def health(self, request):
        checks = {}

        try:
            Product.objects.using("default").first()
            checks["postgres_default"] = "ok"
        except Exception as e:
            checks["postgres_default"] = f"error: {e}"

        for slave in ["slave1", "slave2", "slave3"]:
            try:
                Product.objects.using(slave).first()
                checks[f"postgres_{slave}"] = "ok"
            except Exception as e:
                checks[f"postgres_{slave}"] = f"error: {e}"

        try:
            cache.set("_health_check", "ok", timeout=5)
            val = cache.get("_health_check")
            checks["redis"] = "ok" if val == "ok" else "error: value mismatch"
        except Exception as e:
            checks["redis"] = f"error: {e}"

        healthy = all(v == "ok" for v in checks.values())
        return Response(
            {"status": "healthy" if healthy else "degraded", "checks": checks},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @action(detail=False, methods=["post"])
    def bulk_create(self, request):
        products_data = request.data if isinstance(request.data, list) else [request.data]
        created = []
        with transaction.atomic():
            for data in products_data:
                serializer = self.get_serializer(data=data)
                serializer.is_valid(raise_exception=True)
                product = serializer.save()
                created.append(product)
        return Response(ProductSerializer(created, many=True).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def adjust_stock(self, request, pk=None):
        product = self.get_object()
        delta = request.data.get("delta", 0)
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=product.pk)
            product.stock = max(0, product.stock + delta)
            product.save()
        return Response(ProductSerializer(product).data)


class ExternalCallView(APIView):
    def get(self, request):
        url = request.query_params.get("url", settings.EXTERNAL_API_URL)
        timeout = int(request.query_params.get("timeout", 5))
        try:
            resp = requests.get(url, timeout=timeout)
            return Response({
                "status_code": resp.status_code,
                "url": url,
                "response_time_ms": resp.elapsed.total_seconds() * 1000,
            })
        except requests.RequestException as e:
            return Response({"error": str(e), "url": url}, status=status.HTTP_502_BAD_GATEWAY)


class DBTransactionView(APIView):
    def post(self, request):
        operations = request.data.get("operations", 5)
        results = []
        with transaction.atomic():
            for i in range(operations):
                product = Product.objects.create(
                    name=f"Tx Product {i}",
                    description=f"From transaction {i}",
                    price="10.00",
                    stock=i,
                )
                results.append(product.id)
                Product.objects.filter(pk=product.pk).update(description=f"Updated {i}")
        return Response({"created_ids": results, "count": len(results)})


class ThreadedView(APIView):
    def get(self, request):
        num_threads = int(request.query_params.get("threads", 3))
        delay = float(request.query_params.get("delay", 0.5))
        results = []

        def worker(idx):
            time.sleep(delay)
            results.append({"thread": idx, "done": True})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        return Response({"threads": num_threads, "results": results})


class RawSQLView(APIView):
    def get(self, request):
        query = request.query_params.get("query", "SELECT 1 as test")
        with connection.cursor() as cursor:
            cursor.execute(query)
            if cursor.description:
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            else:
                columns = []
                rows = []
        return Response({"query": query, "rows": rows, "row_count": len(rows)})


class MultiDBQueryView(APIView):
    """Execute raw parameterized SQL queries (SELECT, INSERT, UPDATE, DELETE) on any specified database."""
    def get(self, request):
        db = request.query_params.get("db", "default")
        from django.db import connections
        if db not in connections:
            return Response({"error": f"Unknown database: {db}"}, status=status.HTTP_400_BAD_REQUEST)

        with connections[db].cursor() as cursor:
            cursor.execute('SELECT id, name, price, stock FROM api_product ORDER BY id DESC LIMIT 10')
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return Response({"database": db, "action": "SELECT", "rows": rows, "count": len(rows)})

    def post(self, request):
        db = request.data.get("db", request.query_params.get("db", "default"))
        action_type = request.data.get("action", request.query_params.get("action", "insert")).lower()

        from django.db import connections
        if db not in connections:
            return Response({"error": f"Unknown database: {db}"}, status=status.HTTP_400_BAD_REQUEST)

        with connections[db].cursor() as cursor:
            if action_type == "insert":
                cursor.execute(
                    'INSERT INTO api_product (name, description, price, stock, created_at, updated_at) VALUES (%s, %s, %s, %s, NOW(), NOW()) RETURNING id',
                    [f"Raw Item on {db} {int(time.time())}", f"Direct raw query execution on {db}", 19.99, 10]
                )
                new_id = cursor.fetchone()[0]
                return Response({"database": db, "action": "INSERT", "inserted_id": new_id}, status=status.HTTP_201_CREATED)
            elif action_type == "update":
                cursor.execute('UPDATE api_product SET stock = stock + 1, updated_at = NOW() WHERE id IN (SELECT id FROM api_product LIMIT 1)')
                return Response({"database": db, "action": "UPDATE", "rows_affected": cursor.rowcount})
            elif action_type == "delete":
                cursor.execute('DELETE FROM api_product WHERE id IN (SELECT id FROM api_product ORDER BY id DESC LIMIT 1)')
                return Response({"database": db, "action": "DELETE", "rows_affected": cursor.rowcount})
            else:
                cursor.execute('SELECT id, name, price, stock FROM api_product LIMIT 5')
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return Response({"database": db, "action": "SELECT", "rows": rows, "count": len(rows)})



class ManualSpanView(APIView):
    def get(self, request):
        try:
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
        except ImportError:
            return Response({"error": "opentelemetry not installed"}, status=status.HTTP_501_NOT_IMPLEMENTED)

        with tracer.start_as_current_span("manual-span") as span:
            span.set_attribute("custom.attribute", "test-value")
            with tracer.start_as_current_span("child-span") as child:
                child.set_attribute("child.data", "nested")
                time.sleep(0.1)
            time.sleep(0.1)
        return Response({"message": "Manual spans created"})


class ThrottledView(APIView):
    throttle_classes = [BurstRateThrottle]

    def get(self, request):
        return Response({
            "message": "Request allowed (within 5 req/minute rate limit)",
            "status": "allowed",
            "client_ip": request.META.get("REMOTE_ADDR"),
        })



class ProductTemplateView(TemplateView):
    template_name = "products/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["products"] = Product.objects.all()[:20]
        return context


class ProductDetailTemplateView(TemplateView):
    template_name = "products/detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pk = kwargs.get("pk")
        try:
            context["product"] = Product.objects.get(pk=pk)
        except Product.DoesNotExist:
            context["product"] = None
class MultiPartTemplateErrorView(TemplateView):
    template_name = "products/multipart_error.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rendered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        context["products"] = Product.objects.all()[:5]
        return context


PARTIAL_TEMPLATES = {
    "_header": "products/_header.html",
    "_footer": "products/_footer.html",
    "_product_card": "products/_product_card.html",
    "_stock_badge": "products/_stock_badge.html",
    "_sidebar": "products/_sidebar.html",
    "_broken_widget": "products/_broken_widget.html",
}


class PartialTemplateView(TemplateView):
    template_name = "products/_header.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rendered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        context["product"] = Product.objects.order_by("id").first()
        return context

    def get(self, request, *args, **kwargs):
        if self.kwargs.get("name") not in PARTIAL_TEMPLATES:
            return HttpResponseNotFound("Unknown partial")
        return super().get(request, *args, **kwargs)

    def get_template_names(self):
        return [PARTIAL_TEMPLATES[self.kwargs["name"]]]


@api_view(["GET"])
def metrics_summary(request):
    from django.db.models import Count, Sum, Avg
    stats = Product.objects.aggregate(
        total=Count("id"),
        total_stock=Sum("stock"),
        avg_price=Avg("price"),
    )
    return Response(stats)


@api_view(["GET"])
def db_info(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        version = cursor.fetchone()[0]
        cursor.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port()")
        db, user, addr, port = cursor.fetchone()
    return Response({
        "version": version,
        "database": db,
        "user": user,
        "host": addr,
        "port": port,
    })


@csrf_exempt
def webhook_receiver(request):
    if request.method == "POST":
        import json
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            payload = {}
        return JsonResponse({"received": True, "payload": payload})
    return JsonResponse({"error": "POST only"}, status=405)


@api_view(["GET"])
def cache_stats(request):
    return Response({
        "backend": cache.__class__.__name__,
        "keys_sample": list(cache.keys("*")[:10]) if hasattr(cache, "keys") else "N/A",
    })


class DashboardView(TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        endpoints = [
            {"name": "POST Create Product", "url": "/api/products/", "method": "POST", "description": "Create product on default postgres DB", "params": [], "body": '{"name": "New Product", "description": "Created via API", "price": "29.99", "stock": 50}', "category": "Frozen Topology Endpoints"},
            {"name": "Read Slave 1", "url": "/api/products/read-slave1/", "method": "GET", "description": "Query products explicitly from slave1db database", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Read Slave 2", "url": "/api/products/read-slave2/", "method": "GET", "description": "Query products explicitly from slave2db database", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Read Slave 3", "url": "/api/products/read-slave3/", "method": "GET", "description": "Query products explicitly from slave3db database", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Cache (Redis)", "url": "/api/products/cache/", "method": "GET", "description": "Exercise Redis cache get/set logic", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "External HTTP Call", "url": "/api/products/external/", "method": "GET", "description": "Perform HTTP call to external-api container", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Server Error (500)", "url": "/api/products/error/", "method": "GET", "description": "Deliberate RuntimeError for error tracing", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Slow Request (2s)", "url": "/api/products/slow/", "method": "GET", "description": "Artificial 2s latency delay", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Throttling (5 req/min)", "url": "/api/products/throttled/", "method": "GET", "description": "Rate limited to 5 req/min (returns 429 Too Many Requests on 6th request)", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Multi-Part Template Error", "url": "/api/template-error/", "method": "GET", "description": "Multi-part page where Part 3 (_broken_widget.html) throws an exception to demonstrate waterfall error spans", "params": [], "category": "Frozen Topology Endpoints"},
            {"name": "Product List", "url": "/api/products/", "method": "GET", "description": "List all products on default DB", "params": [], "category": "Products"},
            {"name": "Product Detail (DB)", "url": "/api/products/1/", "method": "GET", "description": "Get single product from default DB", "params": [], "category": "Products"},
            {"name": "Health Check", "url": "/api/products/health/", "method": "GET", "description": "Health check for all 4 PostgreSQL instances and Redis", "params": [], "category": "Products"},
            {"name": "Bulk Create", "url": "/api/products/bulk_create/", "method": "POST", "description": "Create multiple products in transaction", "params": [], "body": '[{"name": "A", "price": "1.00", "stock": 1}, {"name": "B", "price": "2.00", "stock": 2}]', "category": "Products"},
            {"name": "Adjust Stock", "url": "/api/products/1/adjust_stock/", "method": "POST", "description": "Atomic stock adjustment with select_for_update", "params": [], "body": '{"delta": -1}', "category": "Products"},
            {"name": "DB Transaction", "url": "/api/db-tx/", "method": "POST", "description": "Multi-statement database transaction", "params": [], "body": '{"operations": 3}', "category": "Tracing / OTel"},
            {"name": "Threaded Work", "url": "/api/threaded/", "method": "GET", "description": "Run work in parallel threads", "params": [{"name": "threads", "default": "3", "desc": "Number of threads"}, {"name": "delay", "default": "0.5", "desc": "Delay per thread (seconds)"}], "category": "Tracing / OTel"},
            {"name": "Raw SQL", "url": "/api/raw-sql/", "method": "GET", "description": "Execute raw SQL query", "params": [{"name": "query", "default": "SELECT 1 as test", "desc": "SQL query to execute"}], "category": "Tracing / OTel"},
            {"name": "Manual OTel Span", "url": "/api/manual-span/", "method": "GET", "description": "Create manual OpenTelemetry spans", "params": [], "category": "Tracing / OTel"},
            {"name": "Metrics Summary", "url": "/api/metrics/", "method": "GET", "description": "Aggregated product statistics", "params": [], "category": "Debug / Info"},
            {"name": "DB Info", "url": "/api/db-info/", "method": "GET", "description": "PostgreSQL connection information", "params": [], "category": "Debug / Info"},
            {"name": "Webhook Receiver", "url": "/api/webhook/", "method": "POST", "description": "Receive webhook payloads", "params": [], "body": '{"event": "test", "data": {"key": "value"}}', "category": "Debug / Info"},
            {"name": "Cache Stats", "url": "/api/cache-stats/", "method": "GET", "description": "Redis cache backend info", "params": [], "category": "Debug / Info"},
            {"name": "Product List (HTML)", "url": "/api/products-tmpl/", "method": "GET", "description": "HTML product list page (base + nested includes)", "params": [], "category": "UI"},
            {"name": "Product Detail (HTML)", "url": "/api/products-tmpl/1/", "method": "GET", "description": "HTML product detail page (extends base)", "params": [], "category": "UI"},
            {"name": "Partial: Header", "url": "/api/partial/_header/", "method": "GET", "description": "Render _header.html partial standalone", "params": [], "category": "UI"},
            {"name": "Partial: Footer", "url": "/api/partial/_footer/", "method": "GET", "description": "Render _footer.html partial standalone", "params": [], "category": "UI"},
            {"name": "Partial: Product Card", "url": "/api/partial/_product_card/", "method": "GET", "description": "Render _product_card.html partial (includes stock badge)", "params": [], "category": "UI"},
            {"name": "Partial: Stock Badge", "url": "/api/partial/_stock_badge/", "method": "GET", "description": "Render _stock_badge.html partial standalone", "params": [], "category": "UI"},
            {"name": "Admin", "url": "/admin/", "method": "GET", "description": "Django Admin (admin/admin)", "params": [], "category": "UI"},
        ]

        categories = {}
        for ep in endpoints:
            cat = ep["category"]
            categories.setdefault(cat, []).append(ep)

        context["categories"] = categories
        return context


class CeleryPublishView(APIView):
    """Publish a background task to Celery without waiting for execution."""

    def post(self, request):
        if not isinstance(request.data, dict):
            return Response({"error": "Expected a JSON object"}, status=status.HTTP_400_BAD_REQUEST)
        raw_val = request.data.get("value", 1)
        raw_fail = request.data.get("fail", False)

        if isinstance(raw_val, bool) or not isinstance(raw_val, (int, str)):
            return Response({"error": "Invalid value: must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            value = int(raw_val)
        except (ValueError, TypeError):
            return Response({"error": "Invalid value: must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(raw_fail, bool):
            fail = raw_fail
        elif isinstance(raw_fail, str) and raw_fail.lower() in ("1", "true", "yes", "0", "false", "no"):
            fail = raw_fail.lower() in ("1", "true", "yes")
        else:
            return Response({"error": "Invalid fail flag: must be a boolean"}, status=status.HTTP_400_BAD_REQUEST)

        from api.celery_app import demo_task

        async_result = demo_task.apply_async(kwargs={"value": value, "fail": fail})
        return Response(
            {
                "task_id": async_result.id,
                "task_name": "sample_app.tasks.demo_task",
                "value": value,
                "fail": fail,
            },
            status=status.HTTP_200_OK,
        )
