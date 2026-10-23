"""Dependency-free Django settings for the isolated Collector A/B demo."""

SECRET_KEY = "local-collector-demo-only"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "config.collector_demo_urls"
INSTALLED_APPS = []
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]
import os

COLLECTOR_DEMO_ENABLED = True
COLLECTOR_DEMO_SOAK = os.environ.get("COLLECTOR_DEMO_SOAK", "false") == "true"
USE_TZ = True

from config.otel import setup_telemetry

setup_telemetry()
