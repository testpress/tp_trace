from django.contrib import admin

from .models import AuditLog, Category, Customer, Order, OrderItem, Product, Review


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "slug", "created_at"]
    search_fields = ["name", "slug"]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "category", "price", "stock", "created_at"]
    list_filter = ["category"]
    search_fields = ["name"]


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "email", "tier", "active", "created_at"]
    list_filter = ["tier", "active"]
    search_fields = ["name", "email"]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "status", "total_amount", "created_at"]
    list_filter = ["status"]
    search_fields = ["customer__name", "id"]
    inlines = [OrderItemInline]


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["id", "product", "customer", "rating", "created_at"]
    list_filter = ["rating"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["id", "action", "entity_type", "entity_id", "timestamp"]
    list_filter = ["action", "entity_type"]
    search_fields = ["entity_id", "details"]
