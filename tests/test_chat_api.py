"""
/chat and /feedback API tests: cache miss and hit, Qdrant failures, uncacheable answers,
prompts that skip the cache, request limits, feedback and the decision log.

Uses FastAPI's TestClient, an in-memory Qdrant, a stand-in embedding model and a stand-in
LLM, so no Qdrant server, PyTorch, model download or API key is needed.
TestClient runs background tasks before it returns, so their effects can be checked.

    python -m pytest tests/test_chat_api.py -v
"""
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np

# Makes "smart_ai_gateway", "main" and the aging tests importable when running from tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The LLM is replaced below, but llm.py must be importable without an API key
os.environ.setdefault("GW_LLM_PROVIDER", "mock")
from smart_ai_gateway.config import get_settings  # noqa: E402
if "smart_ai_gateway.services.llm" not in sys.modules:
    # Settings may have been read by an earlier test module before the line above
    get_settings.cache_clear()

# Installs a stand-in sentence_transformers module (no PyTorch) if the real one is not loaded
import test_semantic_cache_aging  # noqa: E402,F401

from fastapi.testclient import TestClient     # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

import main                                                   # noqa: E402
from smart_ai_gateway.services import semantic_cache as sc    # noqa: E402

DIM = 64


def unit(vector):
    vector = np.asarray(vector, dtype=float)
    return vector / np.linalg.norm(vector)


def random_vector(text: str):
    # The same text always gets the same vector; different texts are far apart (cosine ~0)
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    return unit(np.random.default_rng(seed).normal(size=DIM))


class FakeEmbeddingModel:
    """
    Stands in for the embedding model: a fixed vector per known prompt, otherwise a vector
    made from the prompt's hash. Also counts how many calls run at the same time.
    """
    max_seq_length = 512

    def __init__(self, vectors=None):
        self.vectors = vectors or {}
        self.fail = None

    def encode(self, prompt):
        if self.fail:
            raise self.fail
        return self.vectors.get(prompt, random_vector(prompt))

    def tokenizer(self, text, verbose=True):
        return {"input_ids": text.split()}


class FakeAnswer:
    """
    What llm.generate_answer returns (LLMAnswer): text, cacheable, finish_reason, usage.
    """

    def __init__(self, text, cacheable=True, finish_reason="STOP"):
        self.text, self.cacheable, self.finish_reason = text, cacheable, finish_reason
        self.usage = {"prompt_tokens": 5, "output_tokens": 7, "thinking_tokens": 0,
                      "total_tokens": 12, "model": "fake", "estimated": True}


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.cacheable = True
        self.lock = threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.calls += 1
        if self.cacheable:
            return FakeAnswer(f"answer to: {prompt}")
        return FakeAnswer(f"cut-off answer to: {prompt}", cacheable=False, finish_reason="MAX_TOKENS")


class ChatApiTestCase(unittest.TestCase):
    """
    One app and scheduler for the whole class; a fresh in-memory collection, stand-in
    models and temporary log files for every test. Has no tests of its own.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved_inference = main.scheduler_worker.inference_fn
        cls.llm = FakeLLM()
        main.scheduler_worker.inference_fn = cls.llm
        cls.http = TestClient(main.app)
        cls.http.__enter__()   # runs the lifespan: starts the scheduler

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)
        main.scheduler_worker.inference_fn = cls._saved_inference

    def setUp(self):
        self.llm.calls, self.llm.cacheable = 0, True
        self.tmp = tempfile.mkdtemp()
        self.decision_log = os.path.join(self.tmp, "logs", "decisions.jsonl")
        self.feedback_log = os.path.join(self.tmp, "logs", "feedback.jsonl")

        self.client = QdrantClient(":memory:")
        self.client.create_collection(
            sc.COLLECTION_NAME,
            vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE),
        )
        self._saved = (sc.client, sc._cfg, sc.verifier, sc.embedding_model, main._gateway_cfg)
        sc.client = self.client
        sc.verifier = None
        sc.embedding_model = self.embeddings = FakeEmbeddingModel()
        self.configure(similarity_threshold=0.97, verify_threshold=0.97, ttl_seconds=86400,
                       sliding_ttl=True, max_age_seconds=0, decay_half_life_seconds=0,
                       cache_max_prompt_chars=300, cache_max_prompt_lines=3,
                       skip_personal_data=True, personal_phrases="my manager, for employee",
                       max_paraphrases=5, paraphrase_duplicate_similarity=0.99,
                       feedback_path=self.feedback_log)
        main._gateway_cfg = dataclasses.replace(
            main._gateway_cfg, decision_log_enabled=True, decision_log_path=self.decision_log)

    def tearDown(self):
        sc.client, sc._cfg, sc.verifier, sc.embedding_model, main._gateway_cfg = self._saved
        self.client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, **changes):
        sc._cfg = dataclasses.replace(sc._cfg, **changes)

    def ask(self, prompt, user="alice"):
        response = self.http.post("/chat", json={"user_id": user, "prompt": prompt})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def count(self):
        return self.client.count(sc.COLLECTION_NAME, exact=True).count

    def decisions(self):
        if not os.path.exists(self.decision_log):
            return []
        with open(self.decision_log, encoding="utf-8") as file:
            return [json.loads(line) for line in file]


class ChatApiTest(ChatApiTestCase):

    # --- the normal path ---------------------------------------------------

    def test_miss_is_answered_stored_then_hit(self):
        first = self.ask("how do I reset my password")
        self.assertEqual(first["source"], main.source_name())
        self.assertEqual(first["response"], "answer to: how do I reset my password")
        self.assertEqual(first["usage"]["total_tokens"], 12)
        self.assertIsNone(first["cache_skipped"])
        self.assertEqual(self.count(), 1)   # the background store ran

        second = self.ask("how do I reset my password", user="bob")
        self.assertEqual(second["source"], "semantic_cache")
        self.assertEqual(second["match_type"], "direct")
        self.assertEqual(second["response"], first["response"])
        self.assertIsNone(second["usage"])
        stored_id = self.client.scroll(sc.COLLECTION_NAME)[0][0].id
        self.assertEqual(second["point_id"], stored_id)
        self.assertEqual(self.llm.calls, 1)

    # --- Qdrant failures (C8) -------------------------------------------------

    def test_search_failure_falls_back_to_llm(self):
        with mock.patch.object(self.client, "query_points", side_effect=ConnectionError("Qdrant down")):
            body = self.ask("how do I reset my password")
        self.assertEqual(body["source"], main.source_name())
        self.assertEqual(self.llm.calls, 1)
        self.assertEqual(self.count(), 0)   # the cache is not used while it is failing
        self.assertEqual(self.decisions()[-1]["stage"], "cache_error")

    def test_embedding_failure_falls_back_to_llm(self):
        self.embeddings.fail = RuntimeError("Already borrowed")
        body = self.ask("how do I reset my password")
        self.assertEqual(body["source"], main.source_name())
        self.assertEqual(self.count(), 0)

    def test_store_failure_still_returns_the_answer(self):
        with mock.patch.object(self.client, "upsert", side_effect=ConnectionError("Qdrant down")) as upsert:
            body = self.ask("how do I reset my password")
        self.assertEqual(body["response"], "answer to: how do I reset my password")
        upsert.assert_called_once()
        self.assertEqual(self.count(), 0)

    # --- what is not cached (C9, C11) ---------------------------------------------

    def test_uncacheable_answer_is_returned_but_not_stored(self):
        self.llm.cacheable = False
        body = self.ask("how do I reset my password")
        self.assertEqual(body["response"], "cut-off answer to: how do I reset my password")
        self.assertEqual(self.count(), 0)
        decision = self.decisions()[-1]
        self.assertFalse(decision["cacheable"])
        self.assertFalse(decision["stored"])

    def test_time_sensitive_prompt_is_not_searched_or_stored(self):
        with mock.patch.object(self.client, "query_points", wraps=self.client.query_points) as search:
            body = self.ask("what is on the canteen menu today")
        search.assert_not_called()
        self.assertIn("time-sensitive", body["cache_skipped"])
        self.assertEqual(self.count(), 0)
        decision = self.decisions()[-1]
        self.assertEqual(decision["stage"], "skipped")
        self.assertIn("time-sensitive", decision["skip_reason"])

    def test_ineligible_prompts_are_not_searched_or_stored(self):
        prompts = {
            "my manager said I can work from home on Friday, is that allowed": "personal context",
            "please reset the password for jane.doe@example.com": "email",
            "I am Sarah from finance, how do I submit a claim": "I am",
            "line one\nline two\nline three\nline four": "pasted text",
            "x" * 301: "too long to cache",
        }
        with mock.patch.object(self.client, "query_points", wraps=self.client.query_points) as search:
            for prompt, reason in prompts.items():
                body = self.ask(prompt)
                self.assertIn(reason, body["cache_skipped"], prompt)
        search.assert_not_called()
        self.assertEqual(self.count(), 0)

    # --- request limits (X1) -----------------------------------------------------

    def test_request_limits_come_from_config(self):
        limit = main._gateway_cfg.prompt_max_length
        self.assertEqual(self.http.post("/chat", json={"user_id": "a", "prompt": "p" * limit}).status_code, 200)
        self.assertEqual(self.http.post("/chat", json={"user_id": "a", "prompt": "p" * (limit + 1)}).status_code, 422)
        user_limit = main._gateway_cfg.user_id_max_length
        self.assertEqual(self.http.post("/chat", json={"user_id": "u" * (user_limit + 1), "prompt": "hi"}).status_code, 422)

    # --- feedback ----------------------------------------------------------------

    def test_feedback_deletes_the_entry_and_logs_the_pair(self):
        self.ask("how do I reset my password")
        hit = self.ask("how do I reset my password")
        self.assertEqual(hit["source"], "semantic_cache")

        response = self.http.post("/feedback", json={
            "point_id": hit["point_id"], "prompt": "how do I reset my password", "reason": "wrong system"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "removed")
        self.assertEqual(response.json()["deleted_points"], 1)
        self.assertEqual(self.count(), 0)

        with open(self.feedback_log, encoding="utf-8") as file:
            record = json.loads(file.readlines()[-1])
        self.assertEqual(record["point_id"], hit["point_id"])
        self.assertEqual(record["prompt"], "how do I reset my password")
        self.assertEqual(record["cached_prompt"], "how do I reset my password")
        self.assertEqual(record["reason"], "wrong system")

        # The next ask is a miss again
        self.assertEqual(self.ask("how do I reset my password")["source"], main.source_name())

    def test_feedback_for_unknown_point_is_404(self):
        for point_id in ("00000000-0000-0000-0000-000000000000", "not-an-id"):
            response = self.http.post("/feedback", json={"point_id": point_id, "prompt": "q"})
            self.assertEqual(response.status_code, 404, point_id)

    def test_feedback_when_qdrant_is_down_is_503(self):
        with mock.patch.object(self.client, "retrieve", side_effect=ConnectionError("down")):
            response = self.http.post("/feedback", json={
                "point_id": "00000000-0000-0000-0000-000000000000", "prompt": "q"})
        self.assertEqual(response.status_code, 503)

    # --- decision log ------------------------------------------------------------

    def test_decision_log_records_each_stage(self):
        self.ask("how do I reset my password")
        self.ask("how do I reset my password")
        miss, hit = self.decisions()

        self.assertEqual(miss["stage"], "miss")
        self.assertEqual(miss["source"], main.source_name())
        self.assertEqual(miss["usage"]["total_tokens"], 12)
        self.assertTrue(miss["stored"])
        self.assertIsNotNone(miss["request_id"])
        self.assertEqual(miss["prompt_chars"], len("how do I reset my password"))
        self.assertNotIn("prompt", miss)   # the text itself is not logged
        for phase in ("total", "embed", "search", "llm"):
            self.assertIsInstance(miss["latency_ms"][phase], float, phase)

        self.assertEqual(hit["stage"], "direct_hit")
        self.assertEqual(hit["source"], "semantic_cache")
        self.assertAlmostEqual(hit["top_similarity"], 1.0, places=4)
        self.assertIsNotNone(hit["point_id"])
        self.assertIsNone(hit["latency_ms"]["llm"])

    def test_decision_log_can_be_turned_off(self):
        main._gateway_cfg = dataclasses.replace(main._gateway_cfg, decision_log_enabled=False)
        self.ask("how do I reset my password")
        self.assertEqual(self.decisions(), [])

    def test_decision_log_error_does_not_break_the_request(self):
        # The log path is a folder, so appending to it fails
        main._gateway_cfg = dataclasses.replace(main._gateway_cfg, decision_log_path=self.tmp)
        self.assertEqual(self.ask("how do I reset my password")["source"], main.source_name())


if __name__ == "__main__":
    unittest.main()
