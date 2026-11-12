"""Prometheus measurements without importing the application or its models.

Call ``inference_started``/``inference_finished`` around the inference function,
not around every caller waiting for a shared answer. These count function calls,
not the Gemini SDK's individual HTTP attempts or retries.

Each Metrics instance owns a registry. The module singleton is for one gateway
process; tests can create independent instances. No prompts, users, request IDs,
or arbitrary URL paths become labels.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)


_ROUTES = frozenset({
    "/", "/health", "/chat", "/scheduler/chat", "/feedback", "/metrics",
    "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/static/{path}",
})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})
_OUTCOMES = ("direct_hit", "verified_hit", "llm", "coalesced", "skipped", "error", "cache_error")
_PHASES = {"embed": "embed", "search": "search", "verify": "verify", "llm": "scheduler_roundtrip"}
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)


def _number(value):
    """Keep malformed measurements and NaNs out of counters and histograms."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _provider(value):
    return value if value in ("gemini", "mock") else "unknown"


class Metrics:
    """Best-effort, bounded-label measurements for one gateway process."""

    def __init__(self, registry=None):
        self.registry = registry if registry is not None else CollectorRegistry()
        self.http_requests = Counter(
            "gateway_http_requests_total", "Completed HTTP requests.",
            ("route", "method", "status"), registry=self.registry,
        )
        self.http_duration = Histogram(
            "gateway_http_request_duration_seconds", "HTTP request duration in seconds.",
            ("route", "method"), buckets=_DURATION_BUCKETS, registry=self.registry,
        )
        self.chat_outcomes = Counter(
            "gateway_chat_outcomes_total", "One mutually exclusive outcome per logged chat decision.",
            ("outcome",), registry=self.registry,
        )
        self.chat_phase_duration = Histogram(
            "gateway_chat_phase_duration_seconds",
            "Chat phase duration; scheduler_roundtrip includes queue waiting and inference.",
            ("phase",), buckets=_DURATION_BUCKETS, registry=self.registry,
        )
        self.inference_calls = Counter(
            "gateway_inference_calls_total", "Finished inference function calls, excluding SDK retry attempts.",
            ("provider", "result"), registry=self.registry,
        )
        self.inference_duration = Histogram(
            "gateway_inference_duration_seconds", "Inference function duration, excluding scheduler queue waiting.",
            ("provider",), buckets=_DURATION_BUCKETS, registry=self.registry,
        )
        self.llm_tokens = Counter(
            "gateway_llm_tokens_total",
            "Tokens reported once per successful inference call; mock token counts are estimates.",
            ("kind",), registry=self.registry,
        )
        self.inference_in_progress = Gauge(
            "gateway_inference_in_progress", "Inference function calls currently executing in this process.",
            registry=self.registry,
        )
        self._pending_count = None
        self._queue_client = None
        self.scheduler_queue_depth = Gauge(
            "gateway_scheduler_queue_depth",
            "Pending jobs; Redis depth is shared across targets, so do not sum Redis targets. NaN if unavailable.",
            registry=self.registry,
        )
        self.scheduler_queue_depth.set_function(self._queue_depth)
        self.info = Info("gateway", "Configured provider, model and scheduler backend.", registry=self.registry)
        for outcome in _OUTCOMES:
            self.chat_outcomes.labels(outcome)
        for phase in _PHASES.values():
            self.chat_phase_duration.labels(phase)
        for kind in ("prompt", "output", "thinking"):
            self.llm_tokens.labels(kind)

    def configure(self, worker, provider, model, backend):
        """Attach a worker's scheduler and describe this process's configuration."""
        try:
            self.info.info({"provider": _provider(provider), "model": str(model), "backend": str(backend)})
            if backend == "redis":
                self._pending_count = None
                # The scheduler's connection can block indefinitely. Scraping uses
                # its own pool with bounded timeouts and no retries instead.
                from redis import ConnectionPool, Redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry

                if self._queue_client is not None:
                    self._queue_client.close()
                source = worker.scheduler.redis.connection_pool
                options = dict(source.connection_kwargs)
                options.update(socket_connect_timeout=0.5, socket_timeout=0.5,
                               retry_on_timeout=False, retry=Retry(NoBackoff(), 0))
                self._queue_client = Redis(connection_pool=ConnectionPool(
                    connection_class=source.connection_class, **options,
                ))
                self._pending_count = lambda: int(self._queue_client.get(worker.scheduler.pending_key) or 0)
            else:
                self._pending_count = worker.scheduler.pending_count
        except Exception:
            # Instrumentation must never prevent the gateway from starting.
            pass

    def _queue_depth(self):
        if self._pending_count is None:
            return float("nan")
        try:
            depth = _number(self._pending_count())
            return depth if depth is not None else float("nan")
        except Exception:
            # Redis being unavailable must not make the entire scrape fail.
            return float("nan")

    def observe_http(self, route, method, status, duration):
        """Observe a completed request using a matched route, never a raw URL."""
        try:
            if route == "/static":
                route = "/static/{path}"
            route = route if route in _ROUTES else "unmatched"
            method = method.upper()
            method = method if method in _METHODS else "OTHER"
            status = int(status)
            status = str(status) if 100 <= status <= 599 else "unknown"
            self.http_requests.labels(route, method, status).inc()
            duration = _number(duration)
            if duration is not None:
                self.http_duration.labels(route, method).observe(duration)
        except Exception:
            pass

    def observe_decision(self, record):
        """Observe one decision; token usage belongs to inference_finished only.

        A handled cache failure takes precedence over the successful LLM fallback.
        An LLM failure takes precedence even when the cache also failed earlier.
        """
        try:
            stage, error = record.get("stage"), record.get("error")
            if error and (stage != "cache_error" or error == "llm_unavailable"):
                outcome = "error"
            elif stage == "cache_error":
                outcome = "cache_error"
            elif stage in ("direct_hit", "verified_hit"):
                outcome = stage
            elif record.get("coalesced"):
                outcome = "coalesced"
            elif stage == "skipped" or record.get("skip_reason"):
                outcome = "skipped"
            else:
                outcome = "llm"
            self.chat_outcomes.labels(outcome).inc()
            latency = record.get("latency_ms") or {}
            for key, phase in _PHASES.items():
                value = _number(latency.get(key))
                if value is not None:
                    self.chat_phase_duration.labels(phase).observe(value / 1000)
        except Exception:
            pass

    def inference_started(self, provider):
        """Call immediately before inference_fn; return its monotonic start time."""
        started = time.perf_counter()
        try:
            self.inference_in_progress.inc()
        except Exception:
            pass
        return started

    def inference_finished(self, provider, started, response=None, *, error=False):
        """Call once in finally; response is the raw inference_fn return value."""
        try:
            provider = _provider(provider)
            self.inference_calls.labels(provider, "error" if error else "success").inc()
            elapsed = _number(time.perf_counter() - started)
            if elapsed is not None:
                self.inference_duration.labels(provider).observe(elapsed)
            if not error:
                usage = response.get("usage") if isinstance(response, Mapping) else getattr(response, "usage", None)
                if isinstance(usage, Mapping):
                    for kind in ("prompt", "output", "thinking"):
                        value = _number(usage.get(f"{kind}_tokens"))
                        if value is not None:
                            self.llm_tokens.labels(kind).inc(value)
        except Exception:
            pass
        finally:
            try:
                self.inference_in_progress.dec()
            except Exception:
                pass

    def render(self):
        """Return Prometheus text; use CONTENT_TYPE_LATEST as the HTTP header."""
        return generate_latest(self.registry)


metrics = Metrics()
