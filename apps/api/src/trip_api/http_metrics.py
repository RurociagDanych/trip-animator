"""Collect bounded, process-local HTTP metrics without retaining request data."""

from fastapi import Request
from prometheus_client import CollectorRegistry, Histogram, generate_latest
from starlette.routing import Route


class HttpMetrics:
    """Track time to response headers per route template and status class."""

    def __init__(self) -> None:
        self._registry = CollectorRegistry()
        self._duration = Histogram(
            "trip_api_http_request_duration_seconds",
            "Time until the API produces response headers or raises an unhandled error.",
            ("route", "status_class"),
            registry=self._registry,
        )

    def observe(self, request: Request, status_code: int, duration: float) -> None:
        """Record an HTTP response without labeling any user-supplied URL or method."""
        matched = request.scope.get("route")
        route = matched.path if isinstance(matched, Route) else "unmatched"
        if route != "/metrics":
            self._duration.labels(route, f"{status_code // 100}xx").observe(duration)

    def exposition(self) -> str:
        """Return the Prometheus text for this application's registry only."""
        return generate_latest(self._registry).decode("utf-8")
