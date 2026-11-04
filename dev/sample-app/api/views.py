from decimal import Decimal
import json
import logging
import os
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import AuditLog, Category, Customer, Order, OrderItem, Product, Review
from .serializers import (
    AuditLogSerializer,
    CategorySerializer,
    CustomerSerializer,
    OrderSerializer,
    ProductSerializer,
    ReviewSerializer,
)

logger = logging.getLogger("api.views")

try:
    from opentelemetry import trace
    tracer = trace.get_tracer("sample-app")
except ImportError:
    tracer = None


class BurstRateThrottle(AnonRateThrottle):
    """Custom throttle limiting unauthenticated requests to 5 per minute for testing."""
    scope = "burst_test"
    rate = "5/minute"


_VERIFIED_BUCKETS = set()


class S3StorageView(APIView):
    """API view executing one S3 / boto3 object storage operation (with graceful fallback)."""

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

        res = client.list_objects_v2(Bucket=bucket_name, Prefix="inventory/")
        return len(str(res))

    def get(self, request):
        bucket_name = request.GET.get("bucket", self.DEFAULT_BUCKET)
        key = request.GET.get("key", self.DEFAULT_KEY)
        op = request.GET.get("op", "list").lower()
        if op not in self.VALID_OPS:
            op = "list"

        try:
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


# ==============================================================================
# Model ViewSets: Products, Categories, Customers, Orders, Reviews, AuditLogs
# ==============================================================================


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    @action(detail=True, methods=["get"], url_path="products")
    def products(self, request, pk=None):
        category = self.get_object()
        cache_key = f"category:{category.id}:products"
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response({"cache_hit": True, "category": category.name, "products": cached_data})

        prods = Product.objects.filter(category=category)[:20]
        data = ProductSerializer(prods, many=True).data
        cache.set(cache_key, data, timeout=120)
        return Response({"cache_hit": False, "category": category.name, "products": data})


class CustomerViewSet(viewsets.ModelViewSet):
    queryset = Customer.objects.all()
    serializer_class = CustomerSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        tier = self.request.query_params.get("tier")
        if tier:
            qs = qs.filter(tier=tier.lower())
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(email__icontains=search))
        return qs

    @action(detail=True, methods=["get"], url_path="orders")
    def orders(self, request, pk=None):
        customer = self.get_object()
        orders = Order.objects.filter(customer=customer).prefetch_related("items__product")
        serializer = OrderSerializer(orders, many=True)
        return Response({
            "customer_id": customer.id,
            "customer_name": customer.name,
            "order_count": len(serializer.data),
            "orders": serializer.data,
        })

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        customer = self.get_object()
        agg = Order.objects.filter(customer=customer).aggregate(
            total_spent=Sum("total_amount"),
            order_count=Count("id"),
            avg_order=Avg("total_amount"),
        )
        return Response({
            "customer_id": customer.id,
            "customer_name": customer.name,
            "tier": customer.tier,
            "total_spent": str(agg.get("total_spent") or "0.00"),
            "order_count": agg.get("order_count") or 0,
            "avg_order": str(round(agg.get("avg_order") or 0, 2)),
        })


class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.prefetch_related("items__product", "customer").all()
    serializer_class = OrderSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param.lower())
        return qs

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request):
        """Realistic multi-step e-commerce checkout workflow exercising:
        - DB Select For Update & Atomic Transaction (default DB)
        - DB Read Replicas (stock verify on slave1)
        - External HTTP Downstream Call (payment gateway simulation)
        - Redis Caching & Invalidation
        - Asynchronous Celery Task dispatch
        - Cross-database audit logging on slave2
        - Custom OpenTelemetry Span Attributes
        """
        customer_id = request.data.get("customer_id")
        items_data = request.data.get("items", [])
        shipping_address = request.data.get("shipping_address", "123 Default Street")

        if not customer_id:
            customer = Customer.objects.first()
            if not customer:
                customer = Customer.objects.create(name="Default Guest", email="guest@example.com")
        else:
            try:
                customer = Customer.objects.get(id=customer_id)
            except Customer.DoesNotExist:
                return Response({"error": f"Customer {customer_id} not found"}, status=status.HTTP_404_NOT_FOUND)

        if not items_data:
            # Fallback to first available product if no items provided
            prod = Product.objects.first()
            if not prod:
                prod = Product.objects.create(name="Checkout Widget", price="19.99", stock=50)
            items_data = [{"product_id": prod.id, "quantity": 1}]

        # 1. Verify read replica stock
        try:
            for item in items_data:
                Product.objects.using("slave1").filter(id=item["product_id"]).first()
        except Exception:
            pass

        # 2. Atomic transaction & lock on Primary DB
        total_amount = Decimal("0.00")
        created_items = []
        with transaction.atomic():
            product_ids = [it["product_id"] for it in items_data]
            locked_products = {
                p.id: p for p in Product.objects.select_for_update().filter(id__in=product_ids)
            }

            for it in items_data:
                pid = it["product_id"]
                qty = int(it.get("quantity", 1))
                prod = locked_products.get(pid)
                if not prod or prod.stock < qty:
                    # Restock on demand if stock is depleted for smooth testing
                    if prod:
                        prod.stock += qty + 10
                        prod.save()
                    else:
                        return Response({"error": f"Product {pid} not found"}, status=status.HTTP_400_BAD_REQUEST)

                prod.stock = max(0, prod.stock - qty)
                prod.save()

                item_total = prod.price * qty
                total_amount += item_total
                created_items.append((prod, qty, prod.price))

            order = Order.objects.create(
                customer=customer,
                status="processing",
                total_amount=total_amount,
                shipping_address=shipping_address,
                notes="Automated checkout",
            )
            for prod, qty, price in created_items:
                OrderItem.objects.create(order=order, product=prod, quantity=qty, unit_price=price)

            # 3. Simulate Downstream Payment Gateway Call
            payment_url = getattr(settings, "EXTERNAL_API_URL", "http://localhost:8080/get")
            payment_ok = True
            try:
                resp = requests.get(f"{payment_url}?order_id={order.id}&amount={total_amount}", timeout=3)
                payment_ok = (resp.status_code == 200)
            except Exception:
                payment_ok = True

            if payment_ok:
                order.status = "completed"
                order.save()

        # 4. Cross-DB audit logging on slave2
        try:
            AuditLog.objects.using("slave2").create(
                action="order.checkout",
                entity_type="Order",
                entity_id=str(order.id),
                details=f"Order #{order.id} checked out by {customer.name} for ${total_amount}",
            )
        except Exception:
            pass

        # 5. Redis cache update
        cache.set(f"order:last_checkout:{customer.id}", {"order_id": order.id, "amount": str(total_amount)}, timeout=120)

        # 6. Celery background fulfillment task
        try:
            from api.celery_app import order_fulfillment_task
            order_fulfillment_task.apply_async(kwargs={"order_id": order.id})
        except Exception:
            pass

        # 7. Custom OpenTelemetry span metadata
        if tracer:
            try:
                current_span = trace.get_current_span()
                if current_span and current_span.is_recording():
                    current_span.set_attribute("ecommerce.order_id", order.id)
                    current_span.set_attribute("ecommerce.total_amount", float(total_amount))
                    current_span.set_attribute("ecommerce.item_count", len(created_items))
                    current_span.set_attribute("customer.tier", customer.tier)
            except Exception:
                pass

        return Response({
            "status": "success",
            "order_id": order.id,
            "total_amount": str(total_amount),
            "customer": customer.name,
            "items_count": len(created_items),
            "payment_verified": payment_ok,
            "audit_replicated": True,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status == "cancelled":
            return Response({"error": "Order is already cancelled"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            for item in order.items.select_related("product"):
                prod = Product.objects.select_for_update().get(id=item.product.id)
                prod.stock += item.quantity
                prod.save()
            order.status = "cancelled"
            order.save()

        try:
            AuditLog.objects.using("slave3").create(
                action="order.cancelled",
                entity_type="Order",
                entity_id=str(order.id),
                details=f"Order #{order.id} cancelled; items returned to stock",
            )
        except Exception:
            pass

        cache.delete(f"order:last_checkout:{order.customer_id}")

        return Response({"status": "cancelled", "order_id": order.id, "restocked": True})

    @action(detail=True, methods=["get"], url_path="invoice")
    def invoice(self, request, pk=None):
        order = self.get_object()
        invoice_key = f"invoices/order_{order.id}.json"
        invoice_data = {
            "invoice_number": f"INV-{order.id:06d}",
            "order_id": order.id,
            "customer": order.customer.name,
            "total_amount": str(order.total_amount),
            "date": order.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "items": [
                {"name": it.product.name, "qty": it.quantity, "price": str(it.unit_price)}
                for it in order.items.select_related("product")
            ],
        }

        # Attempt to persist invoice to S3
        s3_status = "simulated"
        try:
            import botocore.session
            session = botocore.session.get_session()
            client = session.create_client(
                "s3",
                region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "mock_access_key"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "mock_secret_key"),
                endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000"),
            )
            bucket = "sample-products-bucket"
            S3StorageView._ensure_bucket(client, bucket)
            client.put_object(Bucket=bucket, Key=invoice_key, Body=json.dumps(invoice_data).encode("utf-8"))
            s3_status = "stored_s3"
        except Exception:
            pass

        cache.set(f"invoice:{order.id}", invoice_data, timeout=300)

        return Response({
            "status": "ready",
            "invoice": invoice_data,
            "storage_status": s3_status,
            "s3_key": invoice_key,
        })


class ReviewViewSet(viewsets.ModelViewSet):
    queryset = Review.objects.select_related("product", "customer").all()
    serializer_class = ReviewSerializer


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditLog.objects.all()[:100]
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        action_param = self.request.query_params.get("action")
        if action_param:
            qs = qs.filter(action=action_param)
        return qs


# ==============================================================================
# ProductViewSet (Preserving All Original Actions + New Extensions)
# ==============================================================================


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer

    @action(detail=False, methods=["get"], url_path="low-stock")
    def low_stock(self, request):
        threshold = int(request.query_params.get("threshold", 15))
        prods = Product.objects.filter(stock__lte=threshold)[:20]
        serializer = self.get_serializer(prods, many=True)
        return Response({"threshold": threshold, "count": len(serializer.data), "products": serializer.data})

    @action(detail=True, methods=["get", "post"], url_path="reviews")
    def reviews(self, request, pk=None):
        product = self.get_object()
        if request.method == "POST":
            rating = int(request.data.get("rating", 5))
            comment = request.data.get("comment", "Great product!")
            customer_id = request.data.get("customer_id")
            customer = Customer.objects.filter(id=customer_id).first() if customer_id else Customer.objects.first()
            if not customer:
                customer = Customer.objects.create(name="Reviewer", email="reviewer@example.com")
            rev = Review.objects.create(product=product, customer=customer, rating=rating, comment=comment)
            cache.delete(f"product:{product.id}")
            return Response(ReviewSerializer(rev).data, status=status.HTTP_201_CREATED)

        reviews_qs = product.reviews.select_related("customer").all()
        return Response(ReviewSerializer(reviews_qs, many=True).data)

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

        primary_product = Product.objects.using("default").first()
        if not primary_product:
            primary_product = Product.objects.using("default").create(name="Primary Source Item", price="19.99", stock=50)

        Product.objects.using("default").filter(pk=primary_product.pk).update(stock=max(0, primary_product.stock - amount))

        slave1_product, _ = Product.objects.using("slave1").get_or_create(
            name=f"Transferred: {primary_product.name}",
            defaults={"price": primary_product.price, "stock": 0, "description": "Received from primary"}
        )
        Product.objects.using("slave1").filter(pk=slave1_product.pk).update(stock=slave1_product.stock + amount)

        Product.objects.using("slave2").create(
            name=f"Audit Transfer {int(time.time())}",
            description=f"Transferred {amount} units from default to slave1",
            price="0.00",
            stock=amount,
        )

        Product.objects.using("slave3").create(
            name=f"Sync Log {int(time.time())}",
            description=f"Sync log for item {primary_product.pk}",
            price="0.00",
            stock=amount,
        )

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
            return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(product)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response({"cache_hit": False, "source": "database", "product": serializer.data})

    @action(detail=True, methods=["get"])
    def slow(self, request, pk=None):
        time.sleep(2)
        try:
            product = self.get_object()
        except Product.DoesNotExist:
            return Response({"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND)
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


# ==============================================================================
# Analytics Endpoints (Multi-table joins, aggregations, flamegraphs)
# ==============================================================================


class SalesSummaryAnalyticsView(APIView):
    """Heavy analytical query aggregating Orders, OrderItems, Products, and Categories."""

    def get(self, request):
        cache_key = "analytics:sales_summary"
        cached = cache.get(cache_key)
        if cached and not request.query_params.get("refresh"):
            return Response({"cache_hit": True, "source": "redis", "data": cached})

        # Aggregation over Orders
        order_stats = Order.objects.aggregate(
            total_revenue=Sum("total_amount"),
            total_orders=Count("id"),
            avg_order_value=Avg("total_amount"),
        )

        # Status breakdown
        status_breakdown = list(
            Order.objects.values("status").annotate(count=Count("id"), revenue=Sum("total_amount"))
        )

        # Category sales via joins
        category_sales = list(
            OrderItem.objects.annotate(line_total=F("quantity") * F("unit_price"))
            .values("product__category__name")
            .annotate(
                total_qty=Sum("quantity"),
                total_sales=Sum("line_total"),
            )
            .order_by("-total_sales")[:5]
        )

        # Customer tier breakdown
        tier_stats = list(
            Order.objects.values("customer__tier").annotate(
                order_count=Count("id"),
                revenue=Sum("total_amount"),
            )
        )

        data = {
            "summary": {
                "total_revenue": str(order_stats.get("total_revenue") or "0.00"),
                "total_orders": order_stats.get("total_orders") or 0,
                "avg_order_value": str(round(order_stats.get("avg_order_value") or 0, 2)),
            },
            "status_breakdown": status_breakdown,
            "top_categories": category_sales,
            "tier_stats": tier_stats,
            "calculated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        cache.set(cache_key, data, timeout=60)
        return Response({"cache_hit": False, "source": "postgres_database", "data": data})


class InventoryHealthAnalyticsView(APIView):
    """Inventory valuation, reorder thresholds, and cross-category health."""

    def get(self, request):
        annotated_prods = Product.objects.annotate(line_val=F("stock") * F("price"))
        stats = annotated_prods.aggregate(
            total_products=Count("id"),
            total_units=Sum("stock"),
            total_valuation=Sum("line_val"),
            low_stock_count=Count("id", filter=Q(stock__lte=10)),
            out_of_stock_count=Count("id", filter=Q(stock=0)),
        )

        by_category = list(
            annotated_prods.values("category__name").annotate(
                count=Count("id"),
                stock=Sum("stock"),
                valuation=Sum("line_val"),
            )
        )

        return Response({
            "valuation_summary": {
                "total_products": stats.get("total_products") or 0,
                "total_units": stats.get("total_units") or 0,
                "total_valuation": str(round(stats.get("total_valuation") or 0, 2)),
                "low_stock_items": stats.get("low_stock_count") or 0,
                "out_of_stock_items": stats.get("out_of_stock_count") or 0,
            },
            "category_distribution": by_category,
        })


class SlowReportAnalyticsView(APIView):
    """Simulates slow analytical reports to exercise APM slow query detection."""

    def get(self, request):
        delay = float(request.query_params.get("delay", 1.2))
        complexity = request.query_params.get("complexity", "medium")

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(%s)", [min(5.0, delay)])

        # CPU processing simulation
        loops = 20000 if complexity == "high" else 5000
        total = sum(i * i for i in range(loops))

        return Response({
            "report_name": "Quarterly Financial Analysis",
            "complexity": complexity,
            "db_sleep_seconds": delay,
            "cpu_iterations": loops,
            "calculated_checksum": total,
            "status": "completed",
        })


# ==============================================================================
# Advanced Cache Patterns (Redis Stampede, Pipeline, Invalidation)
# ==============================================================================


class CacheStampedeView(APIView):
    """Demonstrates cache-aside with mutex/lock to prevent cache stampedes."""

    def get(self, request):
        key = "stampede:hot_resource"
        lock_key = "stampede:lock:hot_resource"
        cached_val = cache.get(key)
        if cached_val is not None:
            return Response({"status": "cache_hit", "value": cached_val, "protected": True})

        # Try to acquire lock
        lock_acquired = cache.add(lock_key, "locked", timeout=5)
        if lock_acquired:
            try:
                # Simulate heavy DB computation
                time.sleep(0.05)
                computed_val = f"heavy_computation_result_{int(time.time())}"
                cache.set(key, computed_val, timeout=60)
                return Response({"status": "lock_owner_recomputed", "value": computed_val})
            finally:
                cache.delete(lock_key)
        else:
            # Standby waiting for lock holder
            time.sleep(0.06)
            cached_val = cache.get(key) or "fallback_stale_value"
            return Response({"status": "waited_for_lock_holder", "value": cached_val})


class CachePipelineView(APIView):
    """Demonstrates Redis multi-key operations and pipelining."""

    def post(self, request):
        items_count = int(request.data.get("count", 10))
        data_to_store = {f"pipeline_item_{i}": f"val_{i}_{time.time()}" for i in range(items_count)}

        try:
            from django_redis import get_redis_connection
            r = get_redis_connection("default")
            pipe = r.pipeline()
            for k, v in data_to_store.items():
                pipe.setex(k, 120, v)
                pipe.lpush("pipeline_keys_history", k)
            pipe.ltrim("pipeline_keys_history", 0, 50)
            pipe.execute()
            storage_type = "redis_pipeline"
        except Exception:
            for k, v in data_to_store.items():
                cache.set(k, v, timeout=120)
            storage_type = "django_cache_fallback"

        return Response({
            "status": "success",
            "operation": storage_type,
            "items_written": items_count,
        })


class CacheInvalidateView(APIView):
    """Invalidate cache keys matching a pattern."""

    def post(self, request):
        pattern = request.data.get("pattern", "order:*")
        deleted_count = 0
        try:
            from django_redis import get_redis_connection
            r = get_redis_connection("default")
            keys = r.keys(f"*{pattern}*")
            if keys:
                deleted_count = r.delete(*keys)
        except Exception:
            deleted_count = 1

        return Response({"status": "invalidated", "pattern": pattern, "keys_deleted": deleted_count})


# ==============================================================================
# Celery Asynchronous Dispatch Views
# ==============================================================================


class CeleryOrderFulfillmentView(APIView):
    def post(self, request):
        order_id = request.data.get("order_id")
        if not order_id:
            order = Order.objects.first()
            order_id = order.id if order else 1

        from api.celery_app import order_fulfillment_task
        task = order_fulfillment_task.apply_async(kwargs={"order_id": order_id})
        return Response({
            "task_id": task.id,
            "task_name": "sample_app.tasks.order_fulfillment_task",
            "order_id": order_id,
            "status": "dispatched",
        })


class CelerySalesReportView(APIView):
    def post(self, request):
        title = request.data.get("title", f"Report {int(time.time())}")
        fail = request.data.get("fail", False)

        from api.celery_app import generate_sales_report_task
        task = generate_sales_report_task.apply_async(kwargs={"title": title, "fail": fail})
        return Response({
            "task_id": task.id,
            "task_name": "sample_app.tasks.generate_sales_report_task",
            "title": title,
            "fail": fail,
            "status": "dispatched",
        })


class CeleryFlakyRetryView(APIView):
    def post(self, request):
        fail_count = int(request.data.get("fail_count", 2))

        from api.celery_app import flaky_retry_task
        task = flaky_retry_task.apply_async(kwargs={"fail_count": fail_count})
        return Response({
            "task_id": task.id,
            "task_name": "sample_app.tasks.flaky_retry_task",
            "fail_count": fail_count,
            "status": "dispatched",
        })


class CeleryTaskStatusView(APIView):
    def get(self, request, task_id):
        from celery.result import AsyncResult
        from api.celery_app import app
        res = AsyncResult(task_id, app=app)
        try:
            state = res.state
            ready = res.ready()
            successful = res.successful() if ready else None
            result_str = str(res.result) if ready else None
        except Exception as e:
            state = "UNKNOWN"
            ready = False
            successful = None
            result_str = str(e)

        return Response({
            "task_id": task_id,
            "state": state,
            "ready": ready,
            "successful": successful,
            "result": result_str,
        })


# ==============================================================================
# Chaos & APM Detection Endpoints (Errors, timeouts, security)
# ==============================================================================


class ChaosHandledExceptionView(APIView):
    """Catches an exception, records on manual span, and returns structured error response."""

    def get(self, request):
        err_msg = request.query_params.get("message", "Simulated internal handled processing exception")
        try:
            raise ValueError(err_msg)
        except Exception as exc:
            if tracer:
                try:
                    span = trace.get_current_span()
                    if span and span.is_recording():
                        span.record_exception(exc)
                        span.set_attribute("chaos.handled_error", True)
                except Exception:
                    pass
            logger.warning(f"Handled chaos exception: {exc}")
            return Response({
                "handled": True,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "recorded_to_apm": True,
            }, status=status.HTTP_200_OK)


class ChaosValidationErrorView(APIView):
    """Returns HTTP 400 Bad Request with field-level validation errors."""

    def get(self, request):
        raise ValidationError({
            "email": ["This field must be a valid corporate email address."],
            "stock": ["Stock quantity cannot be negative for physical fulfillment."],
            "tier": ["Invalid loyalty tier specified."],
        })


class ChaosUnauthorizedView(APIView):
    """Returns HTTP 401 Unauthorized or 403 Forbidden with custom security headers."""

    def get(self, request):
        status_code = int(request.query_params.get("code", 401))
        resp = Response({
            "error": "Authentication credentials were not provided or invalid token passed.",
            "code": status_code,
        }, status=status.HTTP_401_UNAUTHORIZED if status_code == 401 else status.HTTP_403_FORBIDDEN)
        resp["WWW-Authenticate"] = 'Bearer realm="sample-app-auth"'
        return resp


class ChaosDownstreamTimeoutView(APIView):
    """Calls external API with an ultra-short timeout to trigger client timeout and HTTP 504."""

    def get(self, request):
        url = request.query_params.get("url", "http://10.255.255.1/")
        timeout_sec = float(request.query_params.get("timeout", 0.05))
        try:
            requests.get(url, timeout=timeout_sec)
            return Response({"status": "unexpected_success"})
        except requests.exceptions.Timeout:
            return Response({
                "error": "Gateway Timeout: downstream service failed to respond in time",
                "target_url": url,
                "timeout_sec": timeout_sec,
            }, status=status.HTTP_504_GATEWAY_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return Response({
                "error": f"Bad Gateway: {e.__class__.__name__}",
                "detail": str(e),
            }, status=status.HTTP_502_BAD_GATEWAY)


class ChaosDBTimeoutView(APIView):
    """Simulates database query timeout using PostgreSQL statement_timeout."""

    def get(self, request):
        timeout_ms = int(request.query_params.get("timeout_ms", 20))
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
                cursor.execute("SELECT pg_sleep(0.5)")
            return Response({"status": "unexpected_query_success"})
        except Exception as exc:
            return Response({
                "error": "Database Query Timeout",
                "exception": exc.__class__.__name__,
                "timeout_ms": timeout_ms,
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ChaosDeadlockSimulationView(APIView):
    """Simulates an explicit transaction rollback / conflict."""

    def get(self, request):
        try:
            with transaction.atomic():
                Product.objects.create(name="Transient Deadlock Item", price="9.99", stock=1)
                raise RuntimeError("Simulated transaction conflict rollback")
        except RuntimeError as e:
            return Response({
                "status": "transaction_rolled_back",
                "reason": str(e),
                "db_consistent": True,
            }, status=status.HTTP_409_CONFLICT)


# ==============================================================================
# Database Reconciliation & Cross-Instance Operations
# ==============================================================================


class DBReconciliationView(APIView):
    """Cross-checks product inventory across all 4 databases to detect drift."""

    def get(self, request):
        report = {}
        for db in ["default", "slave1", "slave2", "slave3"]:
            try:
                cnt = Product.objects.using(db).count()
                max_id = Product.objects.using(db).order_by("-id").values_list("id", flat=True).first()
                order_cnt = Order.objects.using(db).count()
                report[db] = {"products_count": cnt, "max_product_id": max_id, "orders_count": order_cnt, "status": "online"}
            except Exception as e:
                report[db] = {"status": "error", "error": str(e)}

        return Response({
            "reconciliation_timestamp": time.time(),
            "databases": report,
            "all_online": all(d.get("status") == "online" for d in report.values()),
        })


class InventoryBatchRebalanceView(APIView):
    """Batch updates and balances stock across primary and replicas."""

    def post(self, request):
        base_stock = int(request.data.get("base_stock", 50))
        updates = {}
        for db in ["default", "slave1", "slave2", "slave3"]:
            try:
                updated = Product.objects.using(db).all()[:10].update(stock=base_stock)
                updates[db] = {"rows_updated": updated, "new_stock": base_stock}
            except Exception as e:
                updates[db] = {"error": str(e)}

        cache.delete("products:all_cached")
        return Response({"status": "rebalanced", "updates": updates})


# ==============================================================================
# Core & Existing Tracing / Utility Views
# ==============================================================================


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
            manual_tracer = trace.get_tracer(__name__)
        except ImportError:
            return Response({"error": "opentelemetry not installed"}, status=status.HTTP_501_NOT_IMPLEMENTED)

        with manual_tracer.start_as_current_span("manual-span") as span:
            span.set_attribute("custom.attribute", "test-value")
            with manual_tracer.start_as_current_span("child-span") as child:
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


# ==============================================================================
# HTML Template Views
# ==============================================================================


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
        return context


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


class OrdersTemplateView(TemplateView):
    template_name = "products/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["orders"] = Order.objects.select_related("customer").prefetch_related("items__product")[:15]
        context["products"] = Product.objects.all()[:10]
        return context


class AnalyticsTemplateView(TemplateView):
    template_name = "products/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["products"] = Product.objects.all()[:10]
        context["total_orders"] = Order.objects.count()
        context["total_revenue"] = Order.objects.aggregate(rev=Sum("total_amount")).get("rev") or "0.00"
        return context


# ==============================================================================
# Functional Endpoints & Diagnostics
# ==============================================================================


@api_view(["GET"])
def metrics_summary(request):
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


class RepeatedQueriesView(APIView):
    """Demonstrates N+1 repeated query pattern for APM detection."""

    def get(self, request):
        try:
            count = max(1, min(100, int(request.GET.get("count", "20"))))
        except (ValueError, TypeError):
            count = 20

        results = []
        for i in range(count):
            prod = Product.objects.filter(id=(i % 10) + 1).first()
            if prod:
                results.append(prod.name)

        return Response(
            {
                "status": "ok",
                "queries_executed": count,
                "threshold": 15,
                "repeated_query_detected": count >= 15,
                "items_returned": len(results),
            },
            status=status.HTTP_200_OK,
        )


# ==============================================================================
# Testing Dashboard View
# ==============================================================================


class DashboardView(TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        endpoints = [
            # Frozen Topology Endpoints
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

            # E-Commerce: Orders & Checkout
            {"name": "Checkout Order (Full Workflow)", "url": "/api/orders/checkout/", "method": "POST", "description": "Multi-tier transaction: stock lock, payment call, audit replica write, Redis cache, Celery async task", "params": [], "body": '{"customer_id": 1, "items": [{"product_id": 1, "quantity": 1}], "shipping_address": "789 Observability Way"}', "category": "E-Commerce: Orders & Checkout"},
            {"name": "List Orders", "url": "/api/orders/", "method": "GET", "description": "List all orders with prefetch_related items and customer info", "params": [{"name": "status", "default": "", "desc": "Filter by status: pending, processing, completed, cancelled"}], "category": "E-Commerce: Orders & Checkout"},
            {"name": "Order Detail", "url": "/api/orders/1/", "method": "GET", "description": "Get single order detail with customer tier and item subtotals", "params": [], "category": "E-Commerce: Orders & Checkout"},
            {"name": "Order Invoice (S3 & Redis)", "url": "/api/orders/1/invoice/", "method": "GET", "description": "Generate invoice and upload/check S3 MinIO storage, cache in Redis", "params": [], "category": "E-Commerce: Orders & Checkout"},
            {"name": "Cancel Order", "url": "/api/orders/1/cancel/", "method": "POST", "description": "Atomic order cancellation: restocks inventory, writes audit log to slave3", "params": [], "category": "E-Commerce: Orders & Checkout"},

            # E-Commerce: Customers & Categories
            {"name": "List Customers", "url": "/api/customers/", "method": "GET", "description": "List customers with tier filtering and search", "params": [{"name": "tier", "default": "", "desc": "bronze, silver, gold, platinum"}, {"name": "search", "default": "", "desc": "Search by name or email"}], "category": "E-Commerce: Customers & Categories"},
            {"name": "Customer Stats", "url": "/api/customers/1/stats/", "method": "GET", "description": "Calculate customer lifetime spend, order count, and average order value", "params": [], "category": "E-Commerce: Customers & Categories"},
            {"name": "Customer Orders", "url": "/api/customers/1/orders/", "method": "GET", "description": "Retrieve all orders for a specific customer", "params": [], "category": "E-Commerce: Customers & Categories"},
            {"name": "List Categories", "url": "/api/categories/", "method": "GET", "description": "List categories with annotated product counts", "params": [], "category": "E-Commerce: Customers & Categories"},
            {"name": "Category Products (Cached)", "url": "/api/categories/1/products/", "method": "GET", "description": "Retrieve products in category with Redis caching", "params": [], "category": "E-Commerce: Customers & Categories"},
            {"name": "Product Reviews", "url": "/api/products/1/reviews/", "method": "GET", "description": "List all reviews for product #1", "params": [], "category": "E-Commerce: Customers & Categories"},
            {"name": "Post Product Review", "url": "/api/products/1/reviews/", "method": "POST", "description": "Submit a review, invalidate cache", "params": [], "body": '{"rating": 5, "comment": "Phenomenal performance!"}', "category": "E-Commerce: Customers & Categories"},
            {"name": "Low Stock Products", "url": "/api/products/low-stock/", "method": "GET", "description": "Find products with inventory below threshold", "params": [{"name": "threshold", "default": "15", "desc": "Stock threshold"}], "category": "E-Commerce: Customers & Categories"},
            {"name": "Audit Logs", "url": "/api/audit-logs/", "method": "GET", "description": "Query recent audit logs with action filter", "params": [{"name": "action", "default": "", "desc": "Action name"}], "category": "E-Commerce: Customers & Categories"},

            # Analytics & Heavy Queries
            {"name": "Sales Summary Analytics", "url": "/api/analytics/sales-summary/", "method": "GET", "description": "Heavy join aggregation across Orders, Products, and Categories", "params": [{"name": "refresh", "default": "", "desc": "Set to 1 to bypass Redis cache"}], "category": "Analytics & Heavy Queries"},
            {"name": "Inventory Health Analytics", "url": "/api/analytics/inventory-health/", "method": "GET", "description": "Valuation and stock health calculations", "params": [], "category": "Analytics & Heavy Queries"},
            {"name": "Slow Report (APM Flamegraph)", "url": "/api/analytics/slow-report/", "method": "GET", "description": "Simulate slow analytical query (pg_sleep + CPU crunch)", "params": [{"name": "delay", "default": "1.2", "desc": "Postgres sleep seconds"}, {"name": "complexity", "default": "medium", "desc": "low, medium, high"}], "category": "Analytics & Heavy Queries"},

            # Advanced Cache & Redis Patterns
            {"name": "Cache Stampede Protection", "url": "/api/cache/stampede-demo/", "method": "GET", "description": "Cache-aside pattern with Redis mutex lock to prevent thundering herd", "params": [], "category": "Advanced Cache Patterns"},
            {"name": "Redis Pipeline Batch", "url": "/api/cache/pipeline/", "method": "POST", "description": "Execute batch Redis pipelining (MSET, LPUSH, LTRIM)", "params": [], "body": '{"count": 10}', "category": "Advanced Cache Patterns"},
            {"name": "Invalidate Cache Pattern", "url": "/api/cache/invalidate/", "method": "POST", "description": "Pattern-based Redis key invalidation", "params": [], "body": '{"pattern": "order:*"}', "category": "Advanced Cache Patterns"},

            # Celery Background Tasks
            {"name": "Async Order Fulfillment Task", "url": "/api/celery/order-fulfillment/", "method": "POST", "description": "Dispatch Celery background task: updates order, HTTP shipping call, writes audit log", "params": [], "body": '{"order_id": 1}', "category": "Celery Background Tasks"},
            {"name": "Async Sales Report Task (S3 CSV)", "url": "/api/celery/sales-report/", "method": "POST", "description": "Dispatch Celery task that aggregates sales, generates CSV, and uploads to MinIO S3", "params": [], "body": '{"title": "Automated Nightly Report", "fail": false}', "category": "Celery Background Tasks"},
            {"name": "Flaky Retry Task (Retry Spans)", "url": "/api/celery/flaky/", "method": "POST", "description": "Dispatch Celery task designed to fail and retry before succeeding", "params": [], "body": '{"fail_count": 2}', "category": "Celery Background Tasks"},
            {"name": "Publish Demo Task", "url": "/api/celery/publish/", "method": "POST", "description": "Publish standard demo_task to RabbitMQ", "params": [], "body": '{"value": 10, "fail": false}', "category": "Celery Background Tasks"},

            # Chaos & Error Testing
            {"name": "Handled Exception (Span Error)", "url": "/api/chaos/handled-exception/", "method": "GET", "description": "Catches exception, records it on current OTel span, returns 200 with diagnostics", "params": [], "category": "Chaos & Error Testing"},
            {"name": "Validation Error (400)", "url": "/api/chaos/validation-error/", "method": "GET", "description": "Triggers DRF ValidationError returning HTTP 400 Bad Request with field errors", "params": [], "category": "Chaos & Error Testing"},
            {"name": "Unauthorized / Forbidden (401/403)", "url": "/api/chaos/unauthorized/", "method": "GET", "description": "Triggers 401 or 403 with WWW-Authenticate security headers", "params": [{"name": "code", "default": "401", "desc": "401 or 403"}], "category": "Chaos & Error Testing"},
            {"name": "Downstream Gateway Timeout (504)", "url": "/api/chaos/downstream-timeout/", "method": "GET", "description": "Calls external service with 50ms timeout to trigger HTTP 504 Gateway Timeout", "params": [], "category": "Chaos & Error Testing"},
            {"name": "Postgres Query Timeout (500)", "url": "/api/chaos/db-timeout/", "method": "GET", "description": "Sets PostgreSQL statement_timeout to 20ms and runs pg_sleep to trigger DB error", "params": [], "category": "Chaos & Error Testing"},
            {"name": "Deadlock / Rollback Simulation (409)", "url": "/api/chaos/deadlock-simulation/", "method": "GET", "description": "Simulates transaction abort and rollback returning HTTP 409 Conflict", "params": [], "category": "Chaos & Error Testing"},

            # Distributed DB & Multi-DB
            {"name": "4-DB Reconciliation", "url": "/api/db/reconciliation/", "method": "GET", "description": "Inspect and compare tables across default, slave1, slave2, and slave3", "params": [], "category": "Distributed DB & Multi-DB"},
            {"name": "Batch Rebalance Stock Across DBs", "url": "/api/inventory/batch-rebalance/", "method": "POST", "description": "Synchronize inventory across all 4 database replicas", "params": [], "body": '{"base_stock": 50}', "category": "Distributed DB & Multi-DB"},
            {"name": "Omni Sync 4 DBs", "url": "/api/products/sync-all-dbs/", "method": "POST", "description": "Creates and updates records across default, slave1, slave2, slave3", "params": [], "body": '{"name": "Omni Item", "price": "49.99", "stock": 100}', "category": "Distributed DB & Multi-DB"},
            {"name": "Multi-DB Transfer & Audit", "url": "/api/products/multi-db-transfer/", "method": "POST", "description": "Read primary, update slave1, write audit on slave2, log on slave3, cache Redis", "params": [], "body": '{"amount": 5}', "category": "Distributed DB & Multi-DB"},
            {"name": "Multi-DB SQL Query", "url": "/api/multi-db/", "method": "GET", "description": "Execute query on specific DB alias", "params": [{"name": "db", "default": "default", "desc": "default, slave1, slave2, slave3"}], "category": "Distributed DB & Multi-DB"},

            # Tracing / OTel
            {"name": "DB Transaction", "url": "/api/db-tx/", "method": "POST", "description": "Multi-statement database transaction", "params": [], "body": '{"operations": 3}', "category": "Tracing / OTel"},
            {"name": "Threaded Work", "url": "/api/threaded/", "method": "GET", "description": "Run work in parallel threads", "params": [{"name": "threads", "default": "3", "desc": "Number of threads"}, {"name": "delay", "default": "0.5", "desc": "Delay per thread (seconds)"}], "category": "Tracing / OTel"},
            {"name": "Raw SQL", "url": "/api/raw-sql/", "method": "GET", "description": "Execute raw SQL query", "params": [{"name": "query", "default": "SELECT 1 as test", "desc": "SQL query to execute"}], "category": "Tracing / OTel"},
            {"name": "Manual OTel Span", "url": "/api/manual-span/", "method": "GET", "description": "Create manual OpenTelemetry spans", "params": [], "category": "Tracing / OTel"},
            {"name": "Repeated Queries (N+1)", "url": "/api/repeated-queries/", "method": "GET", "description": "Simulate N+1 query loop for APM detector", "params": [{"name": "count", "default": "20", "desc": "Number of queries"}], "category": "Tracing / OTel"},
            {"name": "S3 Cloud Storage", "url": "/api/s3-storage/", "method": "GET", "description": "Boto3 S3 operation", "params": [{"name": "op", "default": "list", "desc": "list, put, get, delete, head"}], "category": "Tracing / OTel"},

            # Debug / Info
            {"name": "Health Check", "url": "/api/products/health/", "method": "GET", "description": "Health check for all 4 PostgreSQL instances and Redis", "params": [], "category": "Debug / Info"},
            {"name": "Metrics Summary", "url": "/api/metrics/", "method": "GET", "description": "Aggregated product statistics", "params": [], "category": "Debug / Info"},
            {"name": "DB Info", "url": "/api/db-info/", "method": "GET", "description": "PostgreSQL connection information", "params": [], "category": "Debug / Info"},
            {"name": "Webhook Receiver", "url": "/api/webhook/", "method": "POST", "description": "Receive webhook payloads", "params": [], "body": '{"event": "test", "data": {"key": "value"}}', "category": "Debug / Info"},
            {"name": "Cache Stats", "url": "/api/cache-stats/", "method": "GET", "description": "Redis cache backend info", "params": [], "category": "Debug / Info"},

            # UI & Templates
            {"name": "Product List (HTML)", "url": "/api/products-tmpl/", "method": "GET", "description": "HTML product list page (base + nested includes)", "params": [], "category": "UI"},
            {"name": "Product Detail (HTML)", "url": "/api/products-tmpl/1/", "method": "GET", "description": "HTML product detail page (extends base)", "params": [], "category": "UI"},
            {"name": "Orders Overview (HTML)", "url": "/api/orders-tmpl/", "method": "GET", "description": "HTML orders dashboard view with nested items", "params": [], "category": "UI"},
            {"name": "Partial: Header", "url": "/api/partial/_header/", "method": "GET", "description": "Render _header.html partial standalone", "params": [], "category": "UI"},
            {"name": "Partial: Footer", "url": "/api/partial/_footer/", "method": "GET", "description": "Render _footer.html partial standalone", "params": [], "category": "UI"},
            {"name": "Admin", "url": "/admin/", "method": "GET", "description": "Django Admin (admin/admin)", "params": [], "category": "UI"},
        ]

        categories = {}
        for ep in endpoints:
            cat = ep["category"]
            categories.setdefault(cat, []).append(ep)

        context["categories"] = categories
        return context
