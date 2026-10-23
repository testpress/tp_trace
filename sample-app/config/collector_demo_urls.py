from django.urls import path
from api.collector_demo import collector_demo

urlpatterns = [
    path("api/collector-demo/<int:operation>/", collector_demo),
    path("api/collector-demo/catalog/<int:operation>/", collector_demo, {"workload": "catalog"}),
    path("api/collector-demo/checkout/<int:operation>/", collector_demo, {"workload": "checkout"}),
    path("api/collector-demo/search/<int:operation>/", collector_demo, {"workload": "search"}),
]
