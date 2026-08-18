from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

HTTP_REQUESTS = Counter(
    "guardrail_http_requests_total",
    "Guardrail API requests by stable route and status.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "guardrail_http_request_duration_seconds",
    "Guardrail API request latency by stable route.",
    ("method", "route"),
)
HTTP_IN_PROGRESS = Gauge(
    "guardrail_http_requests_in_progress",
    "Guardrail API requests currently executing.",
    ("method",),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"))


def configure_json_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class HttpMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = scope["method"]
        status_code = 500
        started_at = time.perf_counter()
        HTTP_IN_PROGRESS.labels(method=method).inc()

        async def observe_send(message: dict[str, object]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)  # type: ignore[arg-type]

        try:
            await self._app(scope, receive, observe_send)
        finally:
            route_object = scope.get("route")
            route = getattr(route_object, "path", "unmatched")
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
            HTTP_DURATION.labels(method=method, route=route).observe(
                time.perf_counter() - started_at
            )
            HTTP_IN_PROGRESS.labels(method=method).dec()


def prometheus_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
