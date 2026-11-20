"""Isolated metrics tests: no gateway/model imports, network or API calls.

    python -B -m unittest discover -s demo/tests -p test_telemetry.py -v
"""
import math
import types
import unittest
from unittest.mock import patch

from demo.telemetry import Metrics


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics()

    def sample(self, name, labels=None):
        return self.metrics.registry.get_sample_value(name, labels or {})

    def test_registries_are_isolated_and_exclude_default_process_collectors(self):
        other = Metrics()
        self.metrics.observe_decision({"stage": "direct_hit"})
        labels = {"outcome": "direct_hit"}
        self.assertEqual(self.sample("gateway_chat_outcomes_total", labels), 1)
        self.assertEqual(other.registry.get_sample_value("gateway_chat_outcomes_total", labels), 0)
        exposition = self.metrics.render().decode()
        self.assertNotIn("python_info", exposition)
        self.assertNotIn("process_cpu_seconds_total", exposition)

    def test_http_labels_are_bounded_and_durations_use_seconds(self):
        self.metrics.observe_http("/chat", "post", 200, 0.125)
        self.metrics.observe_http("/users/private-123", "CUSTOM-SECRET", 404, 0.25)
        self.metrics.observe_http("/static", "GET", 200, 0.01)
        self.assertEqual(self.sample("gateway_http_requests_total", {"route": "/chat", "method": "POST", "status": "200"}), 1)
        self.assertEqual(self.sample("gateway_http_request_duration_seconds_sum", {"route": "/chat", "method": "POST"}), 0.125)
        self.assertEqual(self.sample("gateway_http_requests_total", {"route": "unmatched", "method": "OTHER", "status": "404"}), 1)
        text = self.metrics.render().decode()
        self.assertNotIn("private-123", text)
        self.assertNotIn("CUSTOM-SECRET", text)
        self.assertIn('route="/static/{path}"', text)

    def test_chat_outcomes_are_mutually_exclusive_with_error_precedence(self):
        cases = [
            ({"stage": "direct_hit"}, "direct_hit"),
            ({"stage": "verified_hit"}, "verified_hit"),
            ({"stage": "below_threshold"}, "llm"),
            ({"stage": "miss", "coalesced": True}, "coalesced"),
            ({"stage": "skipped", "skip_reason": "personal"}, "skipped"),
            ({"stage": "miss", "error": "llm_unavailable"}, "error"),
            ({"stage": "cache_error", "error": "Qdrant disconnected", "source": "gemini"}, "cache_error"),
            ({"stage": "cache_error", "error": "llm_unavailable"}, "error"),
        ]
        for record, outcome in cases:
            previous = self.sample("gateway_chat_outcomes_total", {"outcome": outcome})
            self.metrics.observe_decision(record)
            self.assertEqual(self.sample("gateway_chat_outcomes_total", {"outcome": outcome}), previous + 1)
        totals = [sample.value for metric in self.metrics.registry.collect() for sample in metric.samples if sample.name == "gateway_chat_outcomes_total"]
        self.assertEqual(sum(totals), len(cases))

    def test_decisions_convert_phase_milliseconds_but_do_not_count_tokens(self):
        self.metrics.observe_decision({
            "stage": "miss", "user_id": "private-person", "prompt": "private-prompt",
            "request_id": "private-request", "coalesced": True,
            "latency_ms": {"embed": 125, "search": 0, "verify": None, "llm": 2500, "total": 3000},
            "usage": {"prompt_tokens": 100, "output_tokens": 200},
        })
        self.assertEqual(self.sample("gateway_chat_phase_duration_seconds_sum", {"phase": "embed"}), 0.125)
        self.assertEqual(self.sample("gateway_chat_phase_duration_seconds_count", {"phase": "search"}), 1)
        self.assertEqual(self.sample("gateway_chat_phase_duration_seconds_count", {"phase": "verify"}), 0)
        self.assertEqual(self.sample("gateway_chat_phase_duration_seconds_sum", {"phase": "scheduler_roundtrip"}), 2.5)
        self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": "prompt"}), 0)
        text = self.metrics.render().decode()
        for secret in ("private-person", "private-prompt", "private-request"):
            self.assertNotIn(secret, text)
        self.assertNotIn('phase="total"', text)

    def test_inference_success_counts_one_call_and_usage_once(self):
        answer = types.SimpleNamespace(usage={"prompt_tokens": 42, "output_tokens": 8, "thinking_tokens": 3, "total_tokens": 53})
        with patch("demo.telemetry.time.perf_counter", side_effect=[10, 10.25]):
            started = self.metrics.inference_started("gemini")
            self.assertEqual(self.sample("gateway_inference_in_progress"), 1)
            self.metrics.inference_finished("gemini", started, answer)
        # Shared HTTP callers may log the same usage, but do not create inferences.
        for _ in range(5):
            self.metrics.observe_decision({"stage": "miss", "coalesced": True, "usage": answer.usage})
        self.assertEqual(self.sample("gateway_inference_in_progress"), 0)
        self.assertEqual(self.sample("gateway_inference_calls_total", {"provider": "gemini", "result": "success"}), 1)
        self.assertEqual(self.sample("gateway_inference_duration_seconds_sum", {"provider": "gemini"}), 0.25)
        for kind, value in (("prompt", 42), ("output", 8), ("thinking", 3)):
            self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": kind}), value)
        self.assertIsNone(self.sample("gateway_llm_tokens_total", {"kind": "total"}))

    def test_inference_error_records_duration_and_clears_in_progress(self):
        with patch("demo.telemetry.time.perf_counter", side_effect=[10, 12]):
            started = self.metrics.inference_started("gemini")
            self.metrics.inference_finished("gemini", started, {"usage": {"prompt_tokens": 42}}, error=True)
        self.assertEqual(self.sample("gateway_inference_in_progress"), 0)
        self.assertEqual(self.sample("gateway_inference_calls_total", {"provider": "gemini", "result": "error"}), 1)
        self.assertEqual(self.sample("gateway_inference_duration_seconds_sum", {"provider": "gemini"}), 2)
        self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": "prompt"}), 0)

    def test_queue_callback_reads_fresh_depth_and_survives_failure(self):
        scheduler = types.SimpleNamespace(pending_count=lambda: 4)
        worker = types.SimpleNamespace(scheduler=scheduler)
        self.metrics.configure(worker, "mock", "demo-model", "memory")
        self.assertEqual(self.sample("gateway_scheduler_queue_depth"), 4)
        self.assertEqual(self.sample("gateway_info", {"provider": "mock", "model": "demo-model", "backend": "memory"}), 1)

        def unavailable():
            raise ConnectionError("Redis is unavailable")

        scheduler.pending_count = unavailable
        self.metrics.configure(worker, "mock", "demo-model", "memory")
        self.assertTrue(math.isnan(self.sample("gateway_scheduler_queue_depth")))
        self.assertIn(b"gateway_scheduler_queue_depth NaN", self.metrics.render())

    def test_redis_scrapes_use_their_own_bounded_connection(self):
        from unittest.mock import Mock
        import redis

        scheduler = types.SimpleNamespace(
            pending_count=Mock(side_effect=AssertionError("Must not use unbounded scheduler connection")),
            pending_key="demo:sched:pending",
            redis=types.SimpleNamespace(connection_pool=types.SimpleNamespace(
                connection_class=redis.Connection,
                connection_kwargs={"host": "not-contacted", "port": 6379},
            )),
        )
        client = Mock()
        client.get.return_value = "7"
        with patch("redis.ConnectionPool") as pool, patch("redis.Redis", return_value=client):
            self.metrics.configure(types.SimpleNamespace(scheduler=scheduler), "mock", "demo-model", "redis")
        self.assertLessEqual(pool.call_args.kwargs["socket_timeout"], 1)
        self.assertLessEqual(pool.call_args.kwargs["socket_connect_timeout"], 1)
        self.assertEqual(self.sample("gateway_scheduler_queue_depth"), 7)
        client.get.assert_called_with("demo:sched:pending")
        client.get.side_effect = TimeoutError("test timeout")
        self.assertTrue(math.isnan(self.sample("gateway_scheduler_queue_depth")))
        scheduler.pending_count.assert_not_called()

    def test_redis_connection_options_are_valid_without_opening_a_socket(self):
        import redis
        source = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
        scheduler = types.SimpleNamespace(redis=source, pending_key="demo:sched:pending")
        self.metrics.configure(types.SimpleNamespace(scheduler=scheduler), "mock", "demo-model", "redis")
        self.assertIsNotNone(self.metrics._queue_client)
        connection = self.metrics._queue_client.connection_pool.make_connection()
        self.assertLessEqual(connection.socket_timeout, 1)
        self.assertLessEqual(connection.socket_connect_timeout, 1)
        self.metrics._queue_client.close()
        source.close()

    def test_invalid_measurements_do_not_poison_histograms_or_tokens(self):
        self.metrics.observe_http("/chat", "POST", 200, float("nan"))
        self.metrics.observe_decision({"stage": "miss", "latency_ms": {"embed": -1, "search": float("inf"), "verify": "bad"}})
        started = self.metrics.inference_started("arbitrary-provider")
        self.metrics.inference_finished("arbitrary-provider", started, {"usage": {"prompt_tokens": -1, "output_tokens": "bad", "thinking_tokens": float("nan")}})
        self.assertEqual(self.sample("gateway_http_requests_total", {"route": "/chat", "method": "POST", "status": "200"}), 1)
        self.assertIsNone(self.sample("gateway_http_request_duration_seconds_count", {"route": "/chat", "method": "POST"}))
        for phase in ("embed", "search", "verify"):
            self.assertEqual(self.sample("gateway_chat_phase_duration_seconds_count", {"phase": phase}), 0)
        for kind in ("prompt", "output", "thinking"):
            self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": kind}), 0)
        self.assertNotIn(b"arbitrary-provider", self.metrics.render())

    def test_observer_failures_never_escape_and_in_progress_is_released(self):
        with patch.object(self.metrics.http_requests, "labels", side_effect=RuntimeError("broken metric")):
            self.metrics.observe_http("/chat", "POST", 200, 1)
        with patch.object(self.metrics.chat_outcomes, "labels", side_effect=RuntimeError("broken metric")):
            self.metrics.observe_decision({"stage": "miss"})
        started = self.metrics.inference_started("mock")
        with patch.object(self.metrics.inference_calls, "labels", side_effect=RuntimeError("broken metric")):
            self.metrics.inference_finished("mock", started, "answer")
        self.assertEqual(self.sample("gateway_inference_in_progress"), 0)
        self.metrics.configure(object(), "mock", "demo-model", "memory")


if __name__ == "__main__":
    unittest.main()
