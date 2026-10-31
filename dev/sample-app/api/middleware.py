import json
import logging
import time
import uuid

from django.http import JsonResponse

logger = logging.getLogger("api.middleware")


class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request._request_id = request_id

        start = time.perf_counter()
        response = self.get_response(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        log_data = {
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }
        logger.info(json.dumps(log_data))

        response["X-Request-ID"] = request_id
        response["X-Response-Time"] = f"{duration_ms}ms"
        return response

    def process_exception(self, request, exception):
        logger.exception(
            json.dumps(
                {
                    "request_id": getattr(request, "_request_id", "unknown"),
                    "method": request.method,
                    "path": request.path,
                    "error": str(exception),
                }
            )
        )
        return JsonResponse(
            {"error": "Internal server error"}, status=500
        )


class UserAgentMiddleware:
    """User-Agent inspection middleware with __call__ and process_request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if response is None:
            response = self.get_response(request)
        return self.process_response(request, response)

    def process_request(self, request):
        request._user_agent = request.headers.get("User-Agent", "unknown")
        return None

    def process_response(self, request, response):
        if hasattr(request, "_user_agent"):
            response["X-Client-Agent"] = request._user_agent[:32]
        return response


class DeviceBindingMiddleware:
    """Device binding verification middleware with __call__ and process_request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if response is None:
            response = self.get_response(request)
        return self.process_response(request, response)

    def process_request(self, request):
        request._device_id = request.headers.get("X-Device-ID", "web-client")
        return None

    def process_response(self, request, response):
        return response


class TenantStorageMiddleware:
    """Tenant storage context middleware with __call__ and process_request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if response is None:
            response = self.get_response(request)
        return self.process_response(request, response)

    def process_request(self, request):
        request._tenant_id = request.headers.get("X-Tenant-ID", "default")
        return None

    def process_response(self, request, response):
        return response
