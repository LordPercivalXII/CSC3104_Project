"""Exercise the actual API and scheduler with in-memory fakes, never an LLM API.

The embedding/cache/LLM modules are replaced before main is imported. No model
downloads, network, Redis, Qdrant server or application log files are needed.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import BackgroundTasks
from demo.telemetry import Metrics
from smart_ai_gateway.config import get_settings


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics()
        self.cache = {}
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        sc = ModuleType("smart_ai_gateway.services.semantic_cache")
        sc._cfg = SimpleNamespace(feedback_path="unused.jsonl")
        sc.append_jsonl = lambda *args: self.fail("File logging must stay disabled")
        sc.cache_skip_reason = lambda prompt: "time-sensitive" if "today" in prompt else None
        sc.generate_embedding = lambda prompt: [0.1, 0.2]
        sc.record_hit = lambda *args: None
        sc.report_wrong_match = lambda *args: None
        sc.store_paraphrase = lambda *args: None

        def store(prompt, response, embedding):
            self.cache[prompt] = {
                "response": response, "point_id": "fake-point", "hit_count": 0,
                "match_type": "direct", "similarity_score": 1.0, "effective_score": 1.0,
                "verifier_score": None, "age_seconds": 0,
            }

        def search(prompt, embedding):
            hit = self.cache.get(prompt)
            return hit, {
                "stage": "direct_hit" if hit else "miss", "top_similarity": 1.0 if hit else None,
                "best_score": 1.0 if hit else None, "verifier_score": None,
                "rule_reason": None, "search_ms": 2, "verify_ms": 0,
            }

        sc.store_cache, sc.search_cache_detailed = store, search
        llm = ModuleType("smart_ai_gateway.services.llm")

        class LLMUnavailable(Exception):
            rate_limited = False

        def generate(prompt):
            self.calls.append(prompt)
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("Timed out waiting for test to release inference")
            if prompt == "force inference failure":
                raise LLMUnavailable("test failure")
            return SimpleNamespace(text="answer: " + prompt, cacheable=True, finish_reason="STOP",
                                   usage={"prompt_tokens": 10, "output_tokens": 5, "thinking_tokens": 0})

        llm.generate_answer, llm.LLMUnavailable = generate, LLMUnavailable
        llm.source_name = lambda: "mock_llm"
        self.patches = [
            patch.dict(os.environ, {
                "GW_LLM_PROVIDER": "mock", "GW_GATEWAY_DECISION_LOG_ENABLED": "false",
                "SCHEDULER_BACKEND": "memory", "SCHEDULER_WORKERS": "1",
                "LLM_RATE_LIMIT_PER_MINUTE": "0", "SCHEDULER_COALESCE": "true",
            }),
            patch.dict(sys.modules, {
                "smart_ai_gateway.services.semantic_cache": sc,
                "smart_ai_gateway.services.llm": llm,
            }),
            patch("demo.telemetry.metrics", self.metrics),
            patch("demo.middleware.metrics", self.metrics),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("gateway_demo_test", root / "main.py")
        self.main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.main)
        self.client = TestClient(self.main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def sample(self, name, labels=None):
        return self.metrics.registry.get_sample_value(name, labels or {}) or 0

    def test_miss_then_hit_counts_one_inference_and_one_token_usage(self):
        body = {"user_id": "employee-a", "prompt": "how do I reset a password"}
        first = self.client.post("/chat", json=body)
        second = self.client.post("/chat", json=body)
        self.assertEqual(first.json()["source"], "mock_llm")
        self.assertEqual(second.json()["source"], "semantic_cache")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sample("gateway_chat_outcomes_total", {"outcome": "direct_hit"}), 1)
        self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": "prompt"}), 10)
        self.assertEqual(self.sample("gateway_http_requests_total", {"route": "/chat", "method": "POST", "status": "200"}), 2)
        scrape = self.client.get("/metrics")
        self.assertEqual(scrape.status_code, 200)
        self.assertIn("text/plain", scrape.headers["content-type"])
        self.assertIn("gateway_inference_calls_total", scrape.text)

    def test_validation_and_inference_failure_are_observable_without_caching(self):
        self.assertEqual(self.client.post("/chat", json={}).status_code, 422)
        failure = self.client.post("/chat", json={"user_id": "a", "prompt": "force inference failure"})
        self.assertEqual(failure.status_code, 503)
        self.assertEqual(self.sample("gateway_inference_calls_total", {"provider": "mock", "result": "error"}), 1)
        self.assertEqual(self.sample("gateway_chat_outcomes_total", {"outcome": "error"}), 1)
        self.assertFalse(self.cache)
        self.assertEqual(self.sample("gateway_inference_in_progress"), 0)
        for path in ("/health", "/metrics"):
            self.client.get(path)
            self.assertEqual(self.sample("gateway_http_requests_total", {"route": path, "method": "GET", "status": "200"}), 0)

    def test_simultaneous_duplicate_jobs_count_one_actual_inference(self):
        self.release.clear()
        self.addCleanup(self.release.set)
        worker = self.main.scheduler_worker
        leader = worker.submit("employee-a", "same question", dedup=True)
        self.assertTrue(self.entered.wait(2))
        follower = worker.submit("employee-b", "same question", dedup=True)
        self.release.set()
        self.assertEqual(leader.result(timeout=3)["response"], "answer: same question")
        self.assertTrue(follower.result(timeout=3)["coalesced"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sample("gateway_inference_calls_total", {"provider": "mock", "result": "success"}), 1)
        self.assertEqual(self.sample("gateway_llm_tokens_total", {"kind": "output"}), 5)

    def test_outcome_is_observed_before_cache_background_tasks(self):
        tasks = BackgroundTasks()
        answer = asyncio.run(self.main.chat(
            self.main.ChatRequest(user_id="employee-a", prompt="how to contact IT support"), tasks,
        ))
        self.assertEqual(answer["source"], "mock_llm")
        self.assertFalse(self.cache)
        self.assertEqual(self.sample("gateway_chat_outcomes_total", {"outcome": "llm"}), 1)
        asyncio.run(tasks())
        self.assertTrue(self.cache)
        self.assertEqual(self.sample("gateway_chat_outcomes_total", {"outcome": "llm"}), 1)


if __name__ == "__main__":
    unittest.main()
