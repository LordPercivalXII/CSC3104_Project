"""Observe completed HTTP responses without waiting for cache background tasks."""
import time

from demo.telemetry import metrics


class GatewayMetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path in ("/metrics", "/health", "/ready", "/favicon.ico") or path.startswith("/static/"):
            return await self.app(scope, receive, send)

        started, status, recorded = time.perf_counter(), 500, False

        def record():
            nonlocal recorded
            if recorded:
                return
            recorded = True
            # Route templates avoid a new time series for every unknown URL.
            route = getattr(scope.get("route"), "path", "unmatched")
            metrics.observe_http(route, scope.get("method", "UNKNOWN"), str(status), time.perf_counter() - started)

        async def observed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                record()

        try:
            await self.app(scope, receive, observed_send)
        finally:
            record()
