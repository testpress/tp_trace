from rest_framework import serializers

from .models import AuditLog, Category, Customer, Order, OrderItem, Product, Review


class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "product_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_product_count(self, obj):
        if hasattr(obj, "product_count"):
            return obj.product_count
        return obj.products.count()


class ReviewSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)

    class Meta:
        model = Review
        fields = [
            "id",
            "product",
            "customer",
            "customer_name",
            "rating",
            "comment",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "description",
            "price",
            "stock",
            "category",
            "category_name",
            "reviews",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CustomerSerializer(serializers.ModelSerializer):
    order_count = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id",
            "name",
            "email",
            "tier",
            "active",
            "order_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_order_count(self, obj):
        if hasattr(obj, "order_count"):
            return obj.order_count
        return obj.orders.count()


class OrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "product",
            "product_name",
            "quantity",
            "unit_price",
            "subtotal",
        ]
        read_only_fields = ["id"]

    def get_subtotal(self, obj):
        return str(round(obj.quantity * obj.unit_price, 2))


class OrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_tier = serializers.CharField(source="customer.tier", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "customer",
            "customer_name",
            "customer_tier",
            "status",
            "total_amount",
            "shipping_address",
            "notes",
            "items",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "action",
            "entity_type",
            "entity_id",
            "details",
            "timestamp",
        ]
        read_only_fields = ["id", "timestamp"]
