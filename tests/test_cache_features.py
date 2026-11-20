"""
Cache feature tests: the caching eligibility filter, extra wordings (paraphrases) per
answer, decision details for misses, the sweeper's grace period for other models' entries,
the clear script's confirmation, and the locks around model calls.

Uses an in-memory Qdrant and stand-ins for the models, so no Qdrant server, PyTorch or
model download is needed.

    python -m pytest tests/test_cache_features.py -v
"""
import dataclasses
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# Makes "smart_ai_gateway" and the other tests importable when running from inside tests/
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GW_LLM_PROVIDER", "mock")

from test_semantic_cache_aging import EXACT, T0, CacheTestCase  # noqa: E402
from test_chat_api import ChatApiTestCase, unit                  # noqa: E402

import numpy as np                                              # noqa: E402

from qdrant_client import QdrantClient, models                  # noqa: E402

from smart_ai_gateway.services import cache_aging               # noqa: E402
from smart_ai_gateway.services import semantic_cache as sc      # noqa: E402

GREY = [0.85, 0.53, 0.0, 0.0]   # cosine ~0.85 to EXACT: the verifier decides
OTHER_A = [0.9, 0.0, 0.44, 0.0]  # ~0.90 to EXACT, ~0.81 to OTHER_B
OTHER_B = [0.9, 0.0, 0.0, 0.44]


def load_script(relative_path: str, name: str):
    # The scripts folder is not a package, so load the file directly
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTokenizerModel:
    max_seq_length = 512

    def tokenizer(self, text, verbose=True):
        return {"input_ids": text.split()}


class FakeVerifier:
    def __init__(self, probability):
        self.probability = probability

    def predict(self, pairs):
        return [self.probability for _ in pairs]


# --- eligibility filter (C11) ---------------------------------------------------

class EligibilityTest(unittest.TestCase):

    def setUp(self):
        self._saved = (sc.embedding_model, sc._cfg)
        sc.embedding_model = FakeTokenizerModel()
        self.configure(skip_time_sensitive=True, cache_max_prompt_chars=300, cache_max_prompt_lines=3,
                       skip_personal_data=True,
                       personal_phrases="my manager, my payslip shows, for employee, I'm new here")

    def tearDown(self):
        sc.embedding_model, sc._cfg = self._saved

    def configure(self, **changes):
        sc._cfg = dataclasses.replace(sc._cfg, **changes)

    def assertSkipped(self, prompt, reason):
        skip = sc.cache_skip_reason(prompt)
        self.assertIsNotNone(skip, prompt)
        self.assertIn(reason, skip, prompt)

    def assertCacheable(self, prompt):
        self.assertIsNone(sc.cache_skip_reason(prompt), prompt)

    def test_plain_faq_questions_are_cacheable(self):
        for prompt in ("how do I reset my password", "is parking free for employees",
                       "where can I find my payslip", "I can't log in to my account",
                       "I'm unable to sign in", "upgrade to Windows 11",
                       "error 0x80070005 in Windows Update", "how do I ask my team lead for leave"):
            self.assertCacheable(prompt)

    def test_time_sensitive_still_checked_first(self):
        self.assertSkipped("what is the latest Teams version", "time-sensitive")

    def test_character_limit(self):
        self.assertSkipped("a" * 301, "too long to cache (301 > 300")
        self.assertCacheable("a" * 300)
        self.configure(cache_max_prompt_chars=0)
        self.assertCacheable("a" * 301)

    def test_code_block(self):
        self.assertSkipped("why does this fail ```print(x)```", "code block")
        self.assertCacheable("what is a code review")

    def test_pasted_lines(self):
        self.assertSkipped("summarise:\nline 1\nline 2\nline 3", "pasted text (4 lines > 3)")
        self.assertCacheable("first line\nsecond line\nthird line")
        self.configure(cache_max_prompt_lines=0)
        self.assertCacheable("a\nb\nc\nd\ne")

    def test_email_address(self):
        self.assertSkipped("reset the password for jane.doe@example.com", "email address")
        self.assertCacheable("how do I change my email signature")

    def test_phone_number(self):
        self.assertSkipped("call me back on +65 9123 4567", "phone number")
        self.assertSkipped("my number is (02) 9876-5432", "phone number")
        self.assertCacheable("how do I set up the phone on my desk")
        self.assertCacheable("move the meeting from 10 to 11")

    def test_id_numbers(self):
        self.assertSkipped("what is the status of ticket INC0012345", "ID number")
        self.assertSkipped("leave balance for staff 1234567", "ID number")
        self.assertCacheable("how many days of annual leave do I get after 2 years")

    def test_self_introduction(self):
        self.assertSkipped("I am Sarah from finance, how do I submit a claim", "I am")
        self.assertSkipped("Hi, I'm Tan, can I carry over leave", "I am")
        self.assertCacheable("I am unable to connect to the VPN")
        self.assertCacheable("I'm locked out of my account")

    def test_personal_data_rules_can_be_turned_off(self):
        self.configure(skip_personal_data=False)
        self.assertCacheable("reset the password for jane.doe@example.com")
        self.assertCacheable("I am Sarah from finance, how do I submit a claim")

    def test_personal_phrases(self):
        self.assertSkipped("my manager said I can leave early, is that ok", "personal context (my manager)")
        self.assertSkipped("My payslip shows the wrong tax code", "my payslip shows")
        self.assertSkipped("I’m new here, where is the canteen", "i'm new here")   # curly apostrophe
        self.assertSkipped("leave balance for employee Tan", "for employee")
        # Whole words only: "for employees" is a general question
        self.assertCacheable("is parking free for employees")
        self.configure(personal_phrases="")
        self.assertCacheable("my manager said I can leave early, is that ok")

    def test_config_has_eligibility_settings(self):
        from smart_ai_gateway.config import load_settings
        cfg = load_settings(env={"GW_SEMANTIC_CACHE_CACHE_MAX_PROMPT_CHARS": "120"}).semantic_cache
        self.assertEqual(cfg.cache_max_prompt_chars, 120)
        self.assertIn("my manager", cfg.personal_phrase_list)
        self.assertEqual(cfg.similarity_threshold, 0.97)


# --- decision details for hits and misses ------------------------------------------

class DecisionDetailsTest(CacheTestCase):

    def setUp(self):
        super().setUp()
        self.configure(similarity_threshold=0.90, verify_threshold=0.75, verifier_threshold=0.5)

    def details(self, prompt, vector=EXACT):
        return sc.search_cache_detailed(prompt, vector, now=T0 + 1)

    def test_empty_cache_is_a_plain_miss(self):
        hit, details = self.details("q")
        self.assertIsNone(hit)
        self.assertEqual(details["stage"], "miss")
        self.assertIsNone(details["top_similarity"])
        self.assertIsInstance(details["search_ms"], float)

    def test_direct_and_verified_hits(self):
        sc.store_cache("q", "a", EXACT, now=T0)
        self.assertEqual(self.details("q")[1]["stage"], "direct_hit")

        self.client.delete(sc.COLLECTION_NAME, points_selector=models.FilterSelector(filter=models.Filter()))
        sc.verifier = FakeVerifier(0.8)
        sc.store_cache("cached", "a", GREY, now=T0)
        hit, details = self.details("new")
        self.assertEqual(details["stage"], "verified_hit")
        self.assertAlmostEqual(details["verifier_score"], 0.8)
        self.assertIsInstance(details["verify_ms"], float)

    def test_verifier_rejected(self):
        sc.verifier = FakeVerifier(0.2)
        sc.store_cache("cached", "a", GREY, now=T0)
        hit, details = self.details("new")
        self.assertIsNone(hit)
        self.assertEqual(details["stage"], "verifier_rejected")
        self.assertAlmostEqual(details["verifier_score"], 0.2)
        self.assertAlmostEqual(details["top_similarity"], 0.85, places=2)

    def test_rule_rejected(self):
        sc.store_cache("how do I enable 2FA", "a", EXACT, now=T0)
        hit, details = self.details("how do I disable 2FA")
        self.assertIsNone(hit)
        self.assertEqual(details["stage"], "rule_rejected")
        self.assertIn("enable", details["rule_reason"])

    def test_below_threshold(self):
        sc.store_cache("cached", "a", [0.6, 0.8, 0.0, 0.0], now=T0)
        hit, details = self.details("new")
        self.assertEqual(details["stage"], "below_threshold")
        self.assertAlmostEqual(details["best_score"], 0.6, places=3)

    def test_select_match_keeps_its_old_shape(self):
        hit, reason = sc.select_match("q", [], T0)
        self.assertIsNone(hit)
        self.assertEqual(reason, "no cached entries")


# --- several wordings per answer --------------------------------------------------

class ParaphraseTest(CacheTestCase):

    def setUp(self):
        super().setUp()
        self.configure(similarity_threshold=0.90, verify_threshold=0.75, verifier_threshold=0.5,
                       max_paraphrases=2, paraphrase_duplicate_similarity=0.99, ttl_seconds=0)
        sc.verifier = FakeVerifier(0.9)
        self.root = sc.store_cache("cached wording", "the answer", GREY, now=T0)

    def payload(self, point_id):
        return self.client.retrieve(sc.COLLECTION_NAME, [point_id])[0].payload

    def verified_hit(self, prompt="new wording", vector=EXACT):
        hit = sc.search_cache(prompt, vector, now=T0 + 10)
        self.assertEqual(hit["match_type"], "verified")
        return hit

    def test_verified_hit_stores_the_new_wording(self):
        hit = self.verified_hit()
        new_id = sc.store_paraphrase("new wording", EXACT, hit, now=T0 + 10)

        payload = self.payload(new_id)
        self.assertEqual(payload["prompt"], "new wording")
        self.assertEqual(payload["response"], "the answer")
        self.assertEqual(payload[cache_aging.PARAPHRASE_OF], self.root)
        self.assertEqual(payload[cache_aging.EMBEDDING_MODEL], sc._cfg.embedding_model)
        self.assertEqual(payload[cache_aging.ANSWERED_BY], sc._answered_by)
        # The answer's age still counts from when it was written; the last hit is new
        self.assertEqual(payload[cache_aging.CREATED_AT_TS], T0)
        self.assertEqual(payload[cache_aging.LAST_HIT_AT_TS], T0 + 10)

        # Next time this wording is a direct hit, without the verifier
        sc.verifier = FakeVerifier(0.0)
        again = sc.search_cache("new wording", EXACT, now=T0 + 20)
        self.assertEqual(again["match_type"], "direct")
        self.assertEqual(again["response"], "the answer")

    def test_paraphrases_are_capped_per_answer(self):
        hit = self.verified_hit()
        self.assertIsNotNone(sc.store_paraphrase("wording 1", OTHER_A, hit, now=T0 + 10))
        self.assertIsNotNone(sc.store_paraphrase("wording 2", OTHER_B, hit, now=T0 + 10))
        self.assertIsNone(sc.store_paraphrase("wording 3", EXACT, hit, now=T0 + 10))
        self.assertEqual(self.client.count(sc.COLLECTION_NAME, exact=True).count, 3)

    def test_paraphrase_of_a_paraphrase_points_at_the_original(self):
        first = sc.store_paraphrase("first wording", OTHER_A, self.verified_hit(), now=T0 + 10)
        hit_on_paraphrase = sc.search_cache("first wording", OTHER_A, now=T0 + 20)
        self.assertEqual(hit_on_paraphrase["point_id"], first)
        second = sc.store_paraphrase("second wording", OTHER_B, hit_on_paraphrase, now=T0 + 20)
        self.assertEqual(self.payload(second)[cache_aging.PARAPHRASE_OF], self.root)

    def test_near_duplicate_wording_is_not_stored(self):
        hit = self.verified_hit()
        # The new wording's vector is the same as the stored question's (cosine 1.0)
        self.assertIsNone(sc.store_paraphrase("cached wording!", GREY, hit, now=T0 + 10))

    def test_paraphrases_can_be_turned_off(self):
        self.configure(max_paraphrases=0)
        self.assertIsNone(sc.store_paraphrase("new wording", EXACT, self.verified_hit(), now=T0 + 10))

    def check_feedback_removes_family(self, report_on_paraphrase):
        hit = self.verified_hit()
        paraphrase = sc.store_paraphrase("wording 1", OTHER_A, hit, now=T0 + 10)
        unrelated = sc.store_cache("something else", "other answer", OTHER_B, now=T0)
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "logs" / "feedback.jsonl"

        target = paraphrase if report_on_paraphrase else self.root
        outcome = sc.report_wrong_match(target, "user question", None, path, now=T0 + 30)
        self.assertEqual(outcome["deleted_points"], 2)
        remaining = [p.id for p in self.client.scroll(sc.COLLECTION_NAME)[0]]
        self.assertEqual(remaining, [unrelated])
        self.assertTrue(path.exists())

    def test_feedback_on_the_answer_removes_its_wordings(self):
        self.check_feedback_removes_family(report_on_paraphrase=False)

    def test_feedback_on_a_wording_removes_the_answer_too(self):
        self.check_feedback_removes_family(report_on_paraphrase=True)


class ParaphraseThroughApiTest(ChatApiTestCase):
    """
    /chat stores a new wording after a verified hit, and not after a direct hit.
    """

    def setUp(self):
        super().setUp()
        base, side = np.zeros(64), np.zeros(64)
        base[0], side[1] = 1.0, 1.0
        self.embeddings.vectors = {
            "how do I reset my password": unit(base),
            "I forgot my password, how can I change it": unit(0.85 * base + 0.53 * side),
        }
        self.configure(similarity_threshold=0.97, verify_threshold=0.75, verifier_threshold=0.5)
        sc.verifier = FakeVerifier(0.9)

    def test_verified_hit_adds_a_wording_and_direct_hit_does_not(self):
        h = self
        h.ask("how do I reset my password")              # miss: stored
        h.ask("how do I reset my password")              # direct hit: nothing new
        self.assertEqual(h.count(), 1)

        body = h.ask("I forgot my password, how can I change it")
        self.assertEqual(body["match_type"], "verified")
        self.assertEqual(h.count(), 2)                   # the new wording was stored

        sc.verifier = FakeVerifier(0.0)                  # now matched without the verifier
        again = h.ask("I forgot my password, how can I change it")
        self.assertEqual(again["match_type"], "direct")
        self.assertEqual(h.count(), 2)
        self.assertEqual([d["stage"] for d in h.decisions()],
                         ["miss", "direct_hit", "verified_hit", "direct_hit"])


# --- sweeper grace period for other models' entries (C15) -----------------------------

class SweeperGraceTest(CacheTestCase):

    def setUp(self):
        super().setUp()
        self.configure(ttl_seconds=0, max_age_seconds=0, other_model_grace_seconds=1000)
        self.point = sc.store_cache("q", "mock answer", EXACT, now=T0)
        # The gateway is switched to another LLM: the entry is no longer served
        sc._answered_by = "gemini:some-model"

    def test_other_models_entry_kept_during_grace_period(self):
        self.assertIsNone(sc.search_cache("q", EXACT, now=T0 + 1))
        self.assertEqual(self.sweep(T0 + 999), 0)
        self.assertEqual(self.sweep(T0 + 1001), 1)

    def test_grace_counts_from_the_last_hit(self):
        sc.record_hit(self.point, 0, now=T0 + 800)
        self.assertEqual(self.sweep(T0 + 1500), 0)
        self.assertEqual(self.sweep(T0 + 1801), 1)

    def test_purge_deletes_at_once(self):
        deleted = cache_aging.sweep_expired(self.client, sc._cfg, T0 + 1, sc._answered_by, purge_other_models=True)
        self.assertEqual(deleted, 1)

    def test_grace_zero_deletes_at_once(self):
        self.configure(other_model_grace_seconds=0)
        self.assertEqual(self.sweep(T0 + 1), 1)

    def test_current_models_entries_are_untouched(self):
        sc._answered_by = "test-llm"
        self.assertEqual(cache_aging.sweep_expired(self.client, sc._cfg, T0 + 10**9, "test-llm",
                                                   purge_other_models=True), 0)

    def test_sweep_script_purge_flag(self):
        script = load_script("smart_ai_gateway/scripts/sweep_expired_cache.py", "sweep_script_under_test")
        script._cfg, script._answered_by = sc._cfg, sc._answered_by
        # The script sweeps at time.time(): pretend it is shortly after the entry was stored
        with mock.patch.object(script, "QdrantClient", return_value=self.client), \
                mock.patch.object(self.client, "close"), mock.patch("builtins.print"), \
                mock.patch.object(script.time, "time", return_value=T0 + 1):
            script.main([])                                   # within the grace period: kept
            self.assertEqual(self.count(), 1)
            script.main(["--purge-other-models"])
            self.assertEqual(self.count(), 0)


# --- clearing the cache needs confirmation (C15) -----------------------------------

class ClearScriptTest(unittest.TestCase):

    def load(self):
        return load_script("smart_ai_gateway/services/clear_semantic_cache.py", "clear_script_under_test")

    def test_import_deletes_nothing(self):
        with mock.patch("qdrant_client.QdrantClient") as qdrant:
            self.load()
        qdrant.assert_not_called()

    def test_without_yes_it_asks_and_stops(self):
        script = self.load()
        with mock.patch.object(script, "QdrantClient") as qdrant, mock.patch("builtins.print"):
            self.assertFalse(script.main([], ask=lambda question: "no"))
            self.assertFalse(script.main([], ask=self._no_terminal))
        qdrant.assert_not_called()

    @staticmethod
    def _no_terminal(question):
        raise EOFError

    def test_yes_clears_the_collection(self):
        script = self.load()
        client = QdrantClient(":memory:")
        client.create_collection(sc.COLLECTION_NAME, vectors_config=models.VectorParams(
            size=4, distance=models.Distance.COSINE))
        client.upsert(sc.COLLECTION_NAME, points=[models.PointStruct(id=1, vector=EXACT, payload={})])
        with mock.patch.object(script, "QdrantClient", return_value=client), \
                mock.patch.object(client, "close"), mock.patch("builtins.print"):
            self.assertTrue(script.main([], ask=lambda question: "yes"))
        self.assertEqual(client.count(sc.COLLECTION_NAME, exact=True).count, 0)
        client.close()


# --- locks around model calls (C14) ---------------------------------------------

class ModelLockTest(unittest.TestCase):

    def setUp(self):
        self._saved = (sc.embedding_model, sc.verifier, sc._cfg)

    def tearDown(self):
        sc.embedding_model, sc.verifier, sc._cfg = self._saved

    def test_locks_exist(self):
        self.assertIsInstance(sc._embedding_lock, type(threading.Lock()))
        self.assertIsInstance(sc._verifier_lock, type(threading.Lock()))

    def test_model_calls_hold_their_lock(self):
        test = self

        class CheckingEmbedding(FakeTokenizerModel):
            def encode(self, prompt):
                test.assertTrue(sc._embedding_lock.locked())
                return np.array(EXACT)

            def tokenizer(self, text, verbose=True):
                test.assertTrue(sc._embedding_lock.locked())
                return {"input_ids": text.split()}

        class CheckingVerifier:
            def predict(self, pairs):
                test.assertTrue(sc._verifier_lock.locked())
                return [0.9 for _ in pairs]

        sc.embedding_model, sc.verifier = CheckingEmbedding(), CheckingVerifier()
        sc._cfg = dataclasses.replace(sc._cfg, similarity_threshold=0.9, verify_threshold=0.75,
                                      decay_half_life_seconds=0)
        self.assertEqual(sc.generate_embedding("q"), EXACT)
        self.assertIsNone(sc.cache_skip_reason("how do I reset my password"))
        payload = {"prompt": "cached", "response": "a", **cache_aging.entry_fields(sc._cfg, T0, "x")}
        hit, _ = sc.select_match("new", [(0.8, payload, "id")], T0)
        self.assertEqual(hit["match_type"], "verified")

    def test_embedding_calls_never_overlap(self):
        state = {"running": 0, "most": 0}
        guard = threading.Lock()

        class SlowEmbedding:
            def encode(self, prompt):
                with guard:
                    state["running"] += 1
                    state["most"] = max(state["most"], state["running"])
                time.sleep(0.01)
                with guard:
                    state["running"] -= 1
                return np.array(EXACT)

        sc.embedding_model = SlowEmbedding()
        threads = [threading.Thread(target=sc.generate_embedding, args=("q",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state["most"], 1)


if __name__ == "__main__":
    unittest.main()
