from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    AnalyticsTemplateView,
    AuditLogViewSet,
    CacheInvalidateView,
    CachePipelineView,
    CacheStampedeView,
    CategoryViewSet,
    CeleryFlakyRetryView,
    CeleryOrderFulfillmentView,
    CeleryPublishView,
    CelerySalesReportView,
    CeleryTaskStatusView,
    ChaosDBTimeoutView,
    ChaosDeadlockSimulationView,
    ChaosDownstreamTimeoutView,
    ChaosHandledExceptionView,
    ChaosUnauthorizedView,
    ChaosValidationErrorView,
    CustomerViewSet,
    DBReconciliationView,
    DBTransactionView,
    ExternalCallView,
    InventoryBatchRebalanceView,
    InventoryHealthAnalyticsView,
    ManualSpanView,
    MultiDBQueryView,
    MultiPartTemplateErrorView,
    OrderViewSet,
    OrdersTemplateView,
    PartialTemplateView,
    ProductDetailTemplateView,
    ProductTemplateView,
    ProductViewSet,
    RawSQLView,
    RepeatedQueriesView,
    ReviewViewSet,
    S3StorageView,
    SalesSummaryAnalyticsView,
    SlowReportAnalyticsView,
    ThreadedView,
    ThrottledView,
    cache_stats,
    db_info,
    metrics_summary,
    webhook_receiver,
)

router = DefaultRouter()
router.register(r"products", ProductViewSet)
router.register(r"categories", CategoryViewSet)
router.register(r"customers", CustomerViewSet)
router.register(r"orders", OrderViewSet)
router.register(r"reviews", ReviewViewSet)
router.register(r"audit-logs", AuditLogViewSet)

urlpatterns = [
    path("", include(router.urls)),

    # E-Commerce & Analytics Endpoints
    path("analytics/sales-summary/", SalesSummaryAnalyticsView.as_view(), name="sales-summary-analytics"),
    path("analytics/inventory-health/", InventoryHealthAnalyticsView.as_view(), name="inventory-health-analytics"),
    path("analytics/slow-report/", SlowReportAnalyticsView.as_view(), name="slow-report-analytics"),

    # Advanced Cache Patterns
    path("cache/stampede-demo/", CacheStampedeView.as_view(), name="cache-stampede-demo"),
    path("cache/pipeline/", CachePipelineView.as_view(), name="cache-pipeline"),
    path("cache/invalidate/", CacheInvalidateView.as_view(), name="cache-invalidate"),

    # Celery Background Tasks
    path("celery/publish/", CeleryPublishView.as_view(), name="celery-publish"),
    path("celery/order-fulfillment/", CeleryOrderFulfillmentView.as_view(), name="celery-order-fulfillment"),
    path("celery/sales-report/", CelerySalesReportView.as_view(), name="celery-sales-report"),
    path("celery/flaky/", CeleryFlakyRetryView.as_view(), name="celery-flaky"),
    path("celery/status/<str:task_id>/", CeleryTaskStatusView.as_view(), name="celery-task-status"),

    # Chaos & Error Scenarios
    path("chaos/handled-exception/", ChaosHandledExceptionView.as_view(), name="chaos-handled-exception"),
    path("chaos/validation-error/", ChaosValidationErrorView.as_view(), name="chaos-validation-error"),
    path("chaos/unauthorized/", ChaosUnauthorizedView.as_view(), name="chaos-unauthorized"),
    path("chaos/downstream-timeout/", ChaosDownstreamTimeoutView.as_view(), name="chaos-downstream-timeout"),
    path("chaos/db-timeout/", ChaosDBTimeoutView.as_view(), name="chaos-db-timeout"),
    path("chaos/deadlock-simulation/", ChaosDeadlockSimulationView.as_view(), name="chaos-deadlock-simulation"),

    # Distributed DB & Multi-DB
    path("db/reconciliation/", DBReconciliationView.as_view(), name="db-reconciliation"),
    path("inventory/batch-rebalance/", InventoryBatchRebalanceView.as_view(), name="inventory-batch-rebalance"),

    # Frozen & Core Endpoints
    path("repeated-queries/", RepeatedQueriesView.as_view(), name="repeated-queries"),
    path("n-plus-one/", RepeatedQueriesView.as_view(), name="n-plus-one"),
    path("s3-storage/", S3StorageView.as_view(), name="s3-storage"),
    path("throttled/", ThrottledView.as_view(), name="throttled"),
    path("template-error/", MultiPartTemplateErrorView.as_view(), name="template-error"),
    path("external/", ExternalCallView.as_view(), name="external-call"),
    path("db-tx/", DBTransactionView.as_view(), name="db-transaction"),
    path("threaded/", ThreadedView.as_view(), name="threaded"),
    path("raw-sql/", RawSQLView.as_view(), name="raw-sql"),
    path("multi-db/", MultiDBQueryView.as_view(), name="multi-db"),
    path("manual-span/", ManualSpanView.as_view(), name="manual-span"),
    path("metrics/", metrics_summary, name="metrics-summary"),
    path("db-info/", db_info, name="db-info"),
    path("webhook/", webhook_receiver, name="webhook"),
    path("cache-stats/", cache_stats, name="cache-stats"),
    path("products-tmpl/", ProductTemplateView.as_view(), name="product-list-tmpl"),
    path("products-tmpl/<int:pk>/", ProductDetailTemplateView.as_view(), name="product-detail-tmpl"),
    path("orders-tmpl/", OrdersTemplateView.as_view(), name="orders-tmpl"),
    path("analytics-tmpl/", AnalyticsTemplateView.as_view(), name="analytics-tmpl"),
    path("partial/<str:name>/", PartialTemplateView.as_view(), name="partial-tmpl"),
]
