"""Tests for the gateway glue: cache tiers, fair-share routing, graceful degradation.

No external services needed: Redis is fakeredis, and the semantic cache is a
small in-memory stand-in (no embedding model, no Qdrant).
"""
import unittest
from concurrent.futures import Future

import fakeredis

from caching.cache import ResponseCache
from caching.config import CacheConfig
from gateway.pipeline import Pipeline
from gateway.settings import GatewaySettings
from scheduler.scheduler_worker import SchedulerWorker


class FakeSemantic:
    """Stands in for smart_ai_gateway.services.semantic_cache.

    The "embedding" is the prompt's word list and similarity is word overlap, which is
    enough to check the plumbing (miss -> store -> paraphrase hit).
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.entries = []

    def generate_embedding(self, prompt):
        return sorted(set(prompt.lower().replace("?", "").split()))

    def search_cache(self, embedding):
        best = None
        for words, prompt, response in self.entries:
            a, b = set(words), set(embedding)
            score = len(a & b) / len(a | b)
            if score >= self.threshold and (best is None or score > best["similarity_score"]):
                best = {"response": response, "matched_prompt": prompt, "similarity_score": score}
        return best

    def store_cache(self, prompt, response, embedding):
        self.entries.append((embedding, prompt, response))
        return "id"


class StubWorker:
    """Records submissions and answers immediately."""

    def __init__(self):
        self.calls = []

    def submit(self, user_id, prompt):
        self.calls.append((user_id, prompt))
        future = Future()
        future.set_result({"response": f"scheduled: {prompt}", "source": "mock", "user_id": user_id})
        return future


class BrokenCache:
    async def lookup(self, *args, **kwargs):
        raise RuntimeError("redis exploded")

    async def store(self, *args, **kwargs):
        raise RuntimeError("redis exploded")


def make_pipeline(mode="both", fair=False, worker=None):
    pipe = Pipeline(
        GatewaySettings(cache_mode=mode, fair_share_enabled=fair),
        worker or StubWorker(),
        CacheConfig(),
    )
    if pipe.settings.use_exact:
        pipe.exact = ResponseCache(fakeredis.FakeAsyncRedis(decode_responses=True), CacheConfig())
    if pipe.settings.use_semantic:
        pipe.semantic = FakeSemantic()
    return pipe


class TestExactCache(unittest.IsolatedAsyncioTestCase):
    async def test_hit_after_miss_even_with_trivial_rewording(self):
        pipe = make_pipeline("exact")
        first = await pipe.handle("How do I deploy?", "u", "team-a", "m")
        second = await pipe.handle("how  do I DEPLOY", "u", "team-a", "m")
        self.assertEqual(first["source"], "mock_llm")
        self.assertEqual(second["source"], "redis_cache")
        self.assertEqual(second["response"], first["response"])

    async def test_entries_are_scoped_to_team_and_model(self):
        pipe = make_pipeline("exact")
        await pipe.handle("How do I deploy?", "u", "team-a", "m1")
        other_team = await pipe.handle("How do I deploy?", "u", "team-b", "m1")
        other_model = await pipe.handle("How do I deploy?", "u", "team-a", "m2")
        self.assertEqual(other_team["source"], "mock_llm")
        self.assertEqual(other_model["source"], "mock_llm")

    async def test_counters(self):
        pipe = make_pipeline("exact")
        await pipe.handle("q", "u", "t", "m")  # miss + store
        await pipe.handle("q", "u", "t", "m")  # hit
        stats = await pipe.cache_stats()
        self.assertEqual((stats["hits"], stats["misses"], stats["stores"]), (1, 1, 1))


class TestSemanticCache(unittest.IsolatedAsyncioTestCase):
    async def test_paraphrase_hits(self):
        pipe = make_pipeline("semantic")
        first = await pipe.handle("what is the deploy process for staging", "u", "t", "m")
        second = await pipe.handle("what is the deploy process for staging environment", "u", "t", "m")
        self.assertEqual(first["source"], "mock_llm")
        self.assertEqual(second["source"], "semantic_cache")
        self.assertEqual(second["response"], first["response"])
        self.assertGreater(second["similarity_score"], 0.5)

    async def test_unrelated_prompt_misses(self):
        pipe = make_pipeline("semantic")
        await pipe.handle("what is the deploy process for staging", "u", "t", "m")
        other = await pipe.handle("explain kubernetes pods", "u", "t", "m")
        self.assertEqual(other["source"], "mock_llm")


class TestTierOrderAndModes(unittest.IsolatedAsyncioTestCase):
    async def test_exact_is_checked_before_semantic(self):
        pipe = make_pipeline("both")
        await pipe.handle("same prompt", "u", "t", "m")
        again = await pipe.handle("same prompt", "u", "t", "m")
        self.assertEqual(again["source"], "redis_cache")

    async def test_mode_none_never_caches(self):
        pipe = make_pipeline("none")
        a = await pipe.handle("q", "u", "t", "m")
        b = await pipe.handle("q", "u", "t", "m")
        self.assertEqual((a["source"], b["source"]), ("mock_llm", "mock_llm"))

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            GatewaySettings(cache_mode="bogus")

    def test_status_reports_active_tiers(self):
        status = make_pipeline("exact").status()
        self.assertTrue(status["exact_cache_active"])
        self.assertFalse(status["semantic_cache_active"])


class TestFairShareRouting(unittest.IsolatedAsyncioTestCase):
    async def test_misses_go_through_the_queue_hits_do_not(self):
        worker = StubWorker()
        pipe = make_pipeline("exact", fair=True, worker=worker)
        first = await pipe.handle("q", "alice", "t", "m")
        second = await pipe.handle("q", "alice", "t", "m")
        self.assertEqual(first["response"], "scheduled: q")
        self.assertEqual(second["source"], "redis_cache")
        self.assertEqual(worker.calls, [("alice", "q")])

    async def test_disabled_bypasses_the_queue(self):
        worker = StubWorker()
        pipe = make_pipeline("none", fair=False, worker=worker)
        result = await pipe.handle("q", "alice", "t", "m")
        self.assertEqual(result["response"], "You asked: q")
        self.assertEqual(worker.calls, [])

    async def test_real_scheduler_worker(self):
        worker = SchedulerWorker()
        worker.start()
        try:
            pipe = make_pipeline("none", fair=True, worker=worker)
            result = await pipe.handle("hello", "alice", "t", "m")
        finally:
            worker.stop()
        self.assertEqual(result["response"], "You asked: hello")
        self.assertEqual(result["source"], "mock_llm")


class TestGracefulDegradation(unittest.IsolatedAsyncioTestCase):
    async def test_cache_errors_never_fail_a_request(self):
        pipe = make_pipeline("none")
        pipe.exact = BrokenCache()
        result = await pipe.handle("q", "u", "t", "m")
        self.assertEqual(result["source"], "mock_llm")


if __name__ == "__main__":
    unittest.main()
