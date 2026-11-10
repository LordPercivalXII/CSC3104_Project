"""
Cache aging tests: TTL, sliding TTL, max age, age decay, model tags, sweeping and backfill.

Uses an in-memory Qdrant and stand-ins for the models, so no Qdrant server, PyTorch or
model download is needed. Needs qdrant-client installed.

Run from the tests folder:
    python test_semantic_cache_aging.py
"""
import dataclasses
import os
import sys
import types
import unittest

from qdrant_client import QdrantClient, models

# Makes "smart_ai_gateway" importable when running from inside tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# semantic_cache loads its models on import; the tests pass vectors directly
if "sentence_transformers" not in sys.modules:
    _fake = types.ModuleType("sentence_transformers")
    _fake.SentenceTransformer = lambda name: None
    _fake.CrossEncoder = lambda name: None
    sys.modules["sentence_transformers"] = _fake

from smart_ai_gateway.config import ConfigError, load_settings  # noqa: E402
from smart_ai_gateway.services import cache_aging                # noqa: E402
from smart_ai_gateway.services import semantic_cache as sc       # noqa: E402

T0 = 1_700_000_000.0          # an arbitrary "now" for storing
EXACT = [1.0, 0.0, 0.0, 0.0]  # the query vector in most tests
NEAR = [0.95, 0.31, 0.0, 0.0] # cosine similarity ~0.95 to EXACT


class CacheTestCase(unittest.TestCase):
    """
    In-memory collection, plain settings: fixed 100 s TTL, no decay, no verifier.
    """

    def setUp(self):
        self.client = QdrantClient(":memory:")
        self.client.create_collection(
            sc.COLLECTION_NAME,
            vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE),
        )
        self._saved = (sc.client, sc._cfg, sc.verifier, sc._answered_by)
        sc.client = self.client
        sc.verifier = None
        sc._answered_by = "test-llm"
        self.configure(ttl_seconds=100, sliding_ttl=False, max_age_seconds=0, decay_half_life_seconds=0,
                       similarity_threshold=0.90, verify_threshold=0.90,
                       # Sweep other models' entries straight away (the grace period has its own tests)
                       other_model_grace_seconds=0)

    def tearDown(self):
        sc.client, sc._cfg, sc.verifier, sc._answered_by = self._saved
        self.client.close()

    def configure(self, **changes):
        sc._cfg = dataclasses.replace(sc._cfg, **changes)
        return sc._cfg

    def store_legacy(self, point_id, payload, vector=EXACT):
        # A point written before aging existed: no numeric timestamps and no model tag
        self.client.upsert(sc.COLLECTION_NAME, points=[
            models.PointStruct(id=point_id, vector=vector, payload=payload)
        ], wait=True)

    def sweep(self, now, cfg=None):
        return cache_aging.sweep_expired(self.client, cfg or sc._cfg, now, sc._answered_by)

    def count(self):
        return self.client.count(sc.COLLECTION_NAME, exact=True).count


class CacheAgingTest(CacheTestCase):

    # --- storing -------------------------------------------------------

    def test_store_writes_aging_fields(self):
        point_id = sc.store_cache("q", "a", EXACT, now=T0)
        payload = self.client.retrieve(sc.COLLECTION_NAME, [point_id])[0].payload
        self.assertEqual(payload[cache_aging.CREATED_AT_TS], T0)
        self.assertEqual(payload[cache_aging.LAST_HIT_AT_TS], T0)
        self.assertEqual(payload[cache_aging.HIT_COUNT], 0)
        self.assertEqual(payload[cache_aging.EMBEDDING_MODEL], sc._cfg.embedding_model)
        self.assertEqual(payload[cache_aging.ANSWERED_BY], "test-llm")
        self.assertIn("created_at", payload)

    # --- fixed TTL -----------------------------------------------------

    def test_fresh_entry_is_a_hit(self):
        sc.store_cache("q", "a", EXACT, now=T0)
        hit = sc.search_cache("q", EXACT, now=T0 + 10)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["response"], "a")
        self.assertAlmostEqual(hit["age_seconds"], 10)

    def test_entry_past_ttl_is_a_miss(self):
        sc.store_cache("q", "a", EXACT, now=T0)
        self.assertIsNotNone(sc.search_cache("q", EXACT, now=T0 + 100))
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 101))

    def test_ttl_zero_never_expires(self):
        self.configure(ttl_seconds=0)
        sc.store_cache("q", "a", EXACT, now=T0)
        self.assertIsNotNone(sc.search_cache("q", EXACT, now=T0 + 10**9))

    def test_hit_does_not_extend_fixed_ttl(self):
        point_id = sc.store_cache("q", "a", EXACT, now=T0)
        sc.record_hit(point_id, 0, now=T0 + 90)
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 101))

    # --- sliding TTL and max age ----------------------------------------

    def test_sliding_ttl_extends_on_hit(self):
        self.configure(sliding_ttl=True, max_age_seconds=1000)
        sc.store_cache("q", "a", EXACT, now=T0)

        hit = sc.search_cache("q", EXACT, now=T0 + 90)
        sc.record_hit(hit["point_id"], hit["hit_count"], now=T0 + 90)

        # 180 s after storing, but only 90 s after the last hit
        self.assertIsNotNone(sc.search_cache("q", EXACT, now=T0 + 180))

    def test_sliding_ttl_expires_without_hits(self):
        self.configure(sliding_ttl=True, max_age_seconds=1000)
        sc.store_cache("q", "a", EXACT, now=T0)
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 101))

    def test_max_age_caps_a_popular_entry(self):
        self.configure(sliding_ttl=True, max_age_seconds=250)
        sc.store_cache("q", "a", EXACT, now=T0)
        for t in (T0 + 90, T0 + 180, T0 + 240):
            hit = sc.search_cache("q", EXACT, now=t)
            self.assertIsNotNone(hit, f"expected a hit at +{t - T0:.0f} s")
            sc.record_hit(hit["point_id"], hit["hit_count"], now=t)
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 251))

    def test_record_hit_counts_hits(self):
        point_id = sc.store_cache("q", "a", EXACT, now=T0)
        for t in (T0 + 1, T0 + 2):
            hit = sc.search_cache("q", EXACT, now=t)
            sc.record_hit(hit["point_id"], hit["hit_count"], now=t)
        payload = self.client.retrieve(sc.COLLECTION_NAME, [point_id])[0].payload
        self.assertEqual(payload[cache_aging.HIT_COUNT], 2)
        self.assertEqual(payload[cache_aging.LAST_HIT_AT_TS], T0 + 2)

    # --- model tags and entries from before aging -------------------------

    def test_untagged_entry_never_served(self):
        self.configure(ttl_seconds=0, max_age_seconds=0)
        self.store_legacy(1, {"prompt": "q", "response": "a", cache_aging.CREATED_AT_TS: T0})
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0))

    def test_entry_from_another_model_never_served(self):
        sc.store_cache("q", "a", EXACT, now=T0)
        self.configure(embedding_model="some/other-model")
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 1))

    def test_answer_from_another_llm_never_served(self):
        sc.store_cache("q", "mock answer", EXACT, now=T0)
        sc._answered_by = "gemini:gemini-3.8-flash"
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 1))
        self.assertEqual(self.sweep(T0 + 1), 1)

    # --- age decay ------------------------------------------------------

    def test_decay_prefers_newer_answer(self):
        self.configure(ttl_seconds=0, decay_half_life_seconds=1000, search_candidates=5)
        sc.store_cache("q", "old answer", EXACT, now=T0)
        sc.store_cache("q", "new answer", NEAR, now=T0 + 2000)

        # old: 1.0 x 0.25 (two half-lives); new: ~0.95 x 1.0
        hit = sc.search_cache("q", EXACT, now=T0 + 2000)
        self.assertEqual(hit["response"], "new answer")
        self.assertLess(hit["similarity_score"], 1.0)

    def test_decay_turns_old_match_into_miss(self):
        self.configure(ttl_seconds=0, decay_half_life_seconds=1000)
        sc.store_cache("q", "a", EXACT, now=T0)
        # 1.0 x 0.5^0.1 = 0.933 still passes 0.90; 1.0 x 0.5^0.2 = 0.871 does not
        self.assertIsNotNone(sc.search_cache("q", EXACT, now=T0 + 100))
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 200))

    def test_decay_off_scores_are_plain_similarity(self):
        sc.store_cache("q", "a", NEAR, now=T0)
        hit = sc.search_cache("q", EXACT, now=T0 + 50)
        self.assertEqual(hit["effective_score"], hit["similarity_score"])

    def test_decay_factor(self):
        self.assertEqual(cache_aging.decay_factor(1000, 0), 1.0)
        self.assertAlmostEqual(cache_aging.decay_factor(1000, 1000), 0.5)
        self.assertAlmostEqual(cache_aging.decay_factor(2000, 1000), 0.25)
        self.assertEqual(cache_aging.decay_factor(-5, 1000), 1.0)

    # --- sweeping -------------------------------------------------------

    def test_sweep_deletes_only_expired_and_legacy(self):
        fresh = sc.store_cache("fresh", "a", EXACT, now=T0 + 50)
        sc.store_cache("stale", "a", EXACT, now=T0 - 500)
        self.store_legacy(1, {"prompt": "legacy", "response": "a"})

        deleted = self.sweep(T0 + 60)

        self.assertEqual(deleted, 2)
        remaining = [p.id for p in self.client.scroll(sc.COLLECTION_NAME)[0]]
        self.assertEqual(remaining, [fresh])

    def test_sweep_with_sliding_ttl_keeps_recently_hit(self):
        self.configure(sliding_ttl=True, max_age_seconds=1000)
        sc.store_cache("q", "a", EXACT, now=T0)
        hit = sc.search_cache("q", EXACT, now=T0 + 90)
        sc.record_hit(hit["point_id"], hit["hit_count"], now=T0 + 90)

        self.assertEqual(self.sweep(T0 + 150), 0)
        self.assertEqual(self.sweep(T0 + 191), 1)

    def test_sweep_with_aging_off_removes_only_other_models(self):
        cfg = self.configure(ttl_seconds=0, max_age_seconds=0)
        kept = sc.store_cache("q", "a", EXACT, now=T0)
        self.store_legacy(1, {"prompt": "legacy", "response": "a"})
        self.store_legacy(2, {"prompt": "other", "response": "a", cache_aging.EMBEDDING_MODEL: "some/other-model"})

        self.assertEqual(self.sweep(T0 + 10**9, cfg), 2)
        self.assertEqual([p.id for p in self.client.scroll(sc.COLLECTION_NAME)[0]], [kept])

    # --- backfill and indexes ----------------------------------------------

    def test_backfill_reads_created_at_and_tags_legacy_model(self):
        self.store_legacy(1, {"prompt": "q", "response": "a", "created_at": "2023-11-14T22:13:20+00:00"})
        self.store_legacy(2, {"prompt": "q", "response": "a"})

        self.assertEqual(cache_aging.backfill_timestamps(self.client, sc._cfg), (1, 1))

        payload = self.client.retrieve(sc.COLLECTION_NAME, [1])[0].payload
        self.assertEqual(payload[cache_aging.CREATED_AT_TS], T0)
        self.assertEqual(payload[cache_aging.LAST_HIT_AT_TS], T0)
        self.assertEqual(payload[cache_aging.EMBEDDING_MODEL], cache_aging.LEGACY_EMBEDDING_MODEL)
        self.assertEqual(payload[cache_aging.ANSWERED_BY], cache_aging.LEGACY_ANSWERED_BY)

        # Served only while the gateway still uses the models that made the vector and answer
        sc._answered_by = cache_aging.LEGACY_ANSWERED_BY
        self.configure(embedding_model="some/other-model")
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 10))
        self.configure(embedding_model=cache_aging.LEGACY_EMBEDDING_MODEL)
        self.assertIsNotNone(sc.search_cache("q", EXACT, now=T0 + 10))

    def test_ensure_indexes_runs_twice(self):
        cache_aging.ensure_indexes(self.client, sc.COLLECTION_NAME)
        cache_aging.ensure_indexes(self.client, sc.COLLECTION_NAME)


class AgingConfigTest(unittest.TestCase):

    def test_config_file_has_aging_settings(self):
        cfg = load_settings(env={}).semantic_cache
        self.assertGreaterEqual(cfg.ttl_seconds, 0)
        self.assertIsInstance(cfg.sliding_ttl, bool)

    def test_negative_ttl_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"GW_SEMANTIC_CACHE_TTL_SECONDS": "-1"})

    def test_zero_search_candidates_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"GW_SEMANTIC_CACHE_SEARCH_CANDIDATES": "0"})


if __name__ == "__main__":
    unittest.main()
