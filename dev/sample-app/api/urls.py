from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ProductViewSet,
    ExternalCallView,
    DBTransactionView,
    ThreadedView,
    RawSQLView,
    MultiDBQueryView,
    ManualSpanView,
    ProductTemplateView,
    ProductDetailTemplateView,
    PartialTemplateView,
    metrics_summary,
    db_info,
    webhook_receiver,
    cache_stats,
    ThrottledView,
    MultiPartTemplateErrorView,
    S3StorageView,
    CeleryPublishView,
    RepeatedQueriesView,
)

router = DefaultRouter()
router.register(r"products", ProductViewSet)

urlpatterns = [
    path("", include(router.urls)),
    path("repeated-queries/", RepeatedQueriesView.as_view(), name="repeated-queries"),
    path("n-plus-one/", RepeatedQueriesView.as_view(), name="n-plus-one"),
    path("celery/publish/", CeleryPublishView.as_view(), name="celery-publish"),
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
    path("partial/<str:name>/", PartialTemplateView.as_view(), name="partial-tmpl"),
]
