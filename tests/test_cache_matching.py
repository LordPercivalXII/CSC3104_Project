"""
Cache matching tests: rule checks, the grey-zone verifier, and prompts that bypass the cache.

Uses an in-memory Qdrant and stand-ins for the models, so no Qdrant server, PyTorch or
model download is needed. Needs qdrant-client installed. To measure matching quality
with the real models, run smart_ai_gateway/scripts/evaluate_cache_matching.py instead.

Run from the tests folder:
    python test_cache_matching.py
"""
import os
import sys
import unittest

# Makes "smart_ai_gateway" and the aging tests importable when running from inside tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_semantic_cache_aging import EXACT, T0, CacheTestCase  # noqa: E402

from smart_ai_gateway.config import ConfigError, load_settings  # noqa: E402
from smart_ai_gateway.services import semantic_cache as sc       # noqa: E402
from smart_ai_gateway.services.cache_checks import conflict, time_sensitive  # noqa: E402

GREY = [0.85, 0.53, 0.0, 0.0]   # cosine similarity ~0.85 to EXACT
FAR = [0.6, 0.8, 0.0, 0.0]      # cosine similarity 0.6 to EXACT


class FakeVerifier:
    """
    Stands in for the cross-encoder: a fixed probability per cached prompt.
    """

    def __init__(self, probabilities):
        self.probabilities = probabilities
        self.calls = []

    def predict(self, pairs):
        self.calls.append(list(pairs))
        return [self.probabilities.get(cached, 0.0) for _, cached in pairs]


class FakeEmbeddingModel:
    """
    Stands in for the embedding model's tokenizer: one token per word.
    """
    max_seq_length = 10

    def tokenizer(self, text, verbose=True):
        return {"input_ids": text.split()}


class RuleCheckTest(unittest.TestCase):

    def test_paraphrases_pass(self):
        self.assertIsNone(conflict("how do I reset my password", "I forgot my password, how can I change it"))
        self.assertIsNone(conflict("I can't log in", "I'm unable to sign in"))
        self.assertIsNone(conflict("my Outlook keeps crashing", "Outlook crashes all the time"))

    def test_different_numbers(self):
        self.assertIn("numbers", conflict("fix VPN error 720", "fix VPN error 809"))
        self.assertIn("numbers", conflict("upgrade to Windows 11", "upgrade to Windows 10"))

    def test_identifier_in_one_prompt(self):
        self.assertIn("identifier", conflict("how do I reset my PIN", "how do I reset my password"))
        self.assertIn("identifier", conflict("open report.pdf", "open the report"))
        self.assertIn("identifier", conflict("share a SharePoint folder", "share a folder"))

    def test_identifier_case_does_not_matter(self):
        self.assertIsNone(conflict("connect to the VPN", "connect to the vpn"))

    def test_all_caps_prompt_is_not_all_acronyms(self):
        self.assertIsNone(conflict("HOW DO I RESET MY PASSWORD", "how do i reset my password"))

    def test_negation_in_one_prompt(self):
        self.assertIn("negated", conflict("I can't access the shared drive", "I can access the shared drive"))
        self.assertIn("negated", conflict("the printer is not working", "the printer is working"))

    def test_opposite_words(self):
        self.assertIn("enable / disable", conflict("how do I disable 2FA", "how do I enable 2FA"))
        self.assertIn("install / uninstall", conflict("uninstalling Teams", "installing Teams"))
        self.assertIn("enable / disable", conflict("my laptop won't turn off", "my laptop won't turn on"))
        self.assertIn("log in / log out", conflict("log out of the portal", "log in to the portal"))

    def test_both_opposites_in_one_prompt_is_fine(self):
        self.assertIsNone(conflict("how do I add or remove users", "how do I remove users"))

    # Rule-layer fixes from the review (REVIEW.md C1-C7, C12, C13). Each test has the
    # "must conflict" pairs first, then the "must not conflict" paraphrases.

    def test_c1_turn_on_off_phrase(self):
        self.assertIsNotNone(conflict("turn off notifications on my phone", "turn on notifications on my phone"))
        self.assertIsNotNone(conflict("switch my laptop off", "switch my laptop on"))
        # The preposition "on" alone is not an on/off switch
        self.assertIsNone(conflict("notifications on my phone", "notifications on my phone"))
        self.assertIsNone(conflict("turn on notifications on my phone", "enable notifications on my phone"))

    def test_c2_more_opposite_pairs(self):
        self.assertIn("upgrade / downgrade", conflict("upgrade to Windows 11", "downgrade to Windows 11"))
        self.assertIn("paid / unpaid", conflict("can I take unpaid leave", "can I take paid leave"))
        self.assertIn("grant / revoke", conflict("grant access to the folder", "revoke access to the folder"))
        self.assertIn("pause / resume", conflict("pause the backup job", "resume the backup job"))
        self.assertIn("send / receive", conflict("send a calendar invite", "receive a calendar invite"))
        self.assertIn("send / receive", conflict("I sent an invite", "I receive an invite"))
        self.assertIn("backup / restore", conflict("back up my files", "restore my files"))
        self.assertIn("internal / external", conflict("book an internal meeting", "book an external meeting"))
        self.assertIsNone(conflict("upgrade my laptop", "upgrade my laptop please"))

    def test_c3_product_names(self):
        self.assertIn("identifier", conflict("reset my Outlook password", "reset my Teams password"))
        self.assertIn("identifier", conflict("meeting on Monday", "meeting on Friday"))
        self.assertIsNone(conflict("reset my Outlook password", "how do I reset the password in Outlook"))
        # Plurals of the same name match
        self.assertIsNone(conflict("how do I connect to the VPN", "how do I connect to VPNs"))
        self.assertIsNone(conflict("install Windows updates", "install Windows update"))

    def test_c4_curly_apostrophe(self):
        self.assertIn("negated", conflict("I can’t access the shared drive", "I can access the shared drive"))
        self.assertIsNone(conflict("I can’t access the shared drive", "I can't access the shared drive"))

    def test_c5_more_negations(self):
        self.assertIn("negated", conflict("I have no access to the shared drive", "I have access to the shared drive"))
        self.assertIn("negated", conflict("nobody can see the folder", "everybody can see the folder"))
        self.assertIn("negated", conflict("neither printer works", "the printer works"))
        self.assertIsNone(conflict("I have no access to the drive", "I do not have access to the drive"))

    def test_c6_hyphenated_phrases(self):
        self.assertIn("sign in / sign out", conflict("how do I sign-in to the portal", "how do I sign out of the portal"))
        self.assertIsNone(conflict("how do I sign-in to the portal", "how do I sign in to the portal"))
        self.assertIsNone(conflict("my e-mail is slow", "my email is slow"))
        self.assertIsNone(conflict("the Wi-Fi keeps dropping", "the wifi keeps dropping"))

    def test_c7_time_sensitive_words(self):
        self.assertEqual(time_sensitive("who is the current IT manager"), "current")
        self.assertEqual(time_sensitive("what is the newest laptop model"), "newest")
        self.assertEqual(time_sensitive("any recent outages"), "recent")
        self.assertIsNone(time_sensitive("how do I reset my password"))

    def test_c12_number_order_for_short_prompts(self):
        self.assertIn("order", conflict("move 5 to 10", "move 10 to 5"))
        self.assertIsNone(conflict("move 5 to 10", "move 5 to 10"))
        # A long prompt only compares which numbers appear
        long_a = "I need to know whether the shared drive quota can change from 5 GB to 10 GB for my team"
        long_b = "for my team I need to know whether the shared drive quota can go to 10 GB from 5 GB"
        self.assertIsNone(conflict(long_a, long_b))

    def test_c12_number_words(self):
        self.assertIsNone(conflict("show the top three results", "show the top 3 results"))
        self.assertIn("numbers", conflict("show the top three results", "show the top five results"))
        # Whole words only, and "one" as a pronoun is not a number
        self.assertIsNone(conflict("someone needs access", "someone needs access"))
        self.assertIsNone(conflict("which one should I use", "which should I use"))
        self.assertIn("numbers", conflict("wait ten minutes", "wait 5 minutes"))

    def test_c13_two_factor_and_plurals(self):
        self.assertIsNone(conflict("turn on 2FA", "enable two factor authentication"))
        self.assertIsNone(conflict("set up MFA", "set up multi-factor authentication"))
        self.assertIn("numbers", conflict("enable 2FA", "enable 3 factor"))

    def test_c13_locked_state_vs_unlock_action(self):
        self.assertIsNone(conflict("my account is locked", "unlock my account"))
        self.assertIsNone(conflict("I'm locked out of my account", "how do I unlock my account"))
        self.assertIn("lock / unlock", conflict("lock my screen", "unlock my screen"))
        self.assertIn("lock / unlock", conflict("how do I lock my account", "how do I unlock my account"))

    def test_time_sensitive(self):
        self.assertEqual(time_sensitive("what is on the menu today"), "today")
        self.assertEqual(time_sensitive("who is on call this week"), "this week")
        self.assertEqual(time_sensitive("what's the latest Teams version"), "latest")
        self.assertIsNone(time_sensitive("how do I reset my password"))


class VerifierTest(CacheTestCase):

    def setUp(self):
        super().setUp()
        # 0.90 and up: direct hit; 0.75 to 0.90: verifier decides
        self.configure(similarity_threshold=0.90, verify_threshold=0.75, verifier_threshold=0.5)

    def test_direct_hit_skips_verifier(self):
        sc.verifier = FakeVerifier({})
        sc.store_cache("q", "a", EXACT, now=T0)
        hit = sc.search_cache("q", EXACT, now=T0 + 1)
        self.assertEqual(hit["match_type"], "direct")
        self.assertIsNone(hit["verifier_score"])
        self.assertEqual(sc.verifier.calls, [])

    def test_grey_zone_hit_when_verifier_agrees(self):
        sc.verifier = FakeVerifier({"cached": 0.8})
        sc.store_cache("cached", "a", GREY, now=T0)
        hit = sc.search_cache("new", EXACT, now=T0 + 1)
        self.assertEqual(hit["match_type"], "verified")
        self.assertAlmostEqual(hit["verifier_score"], 0.8)
        self.assertEqual(sc.verifier.calls, [[("new", "cached")]])

    def test_grey_zone_miss_when_verifier_disagrees(self):
        sc.verifier = FakeVerifier({"cached": 0.2})
        sc.store_cache("cached", "a", GREY, now=T0)
        self.assertIsNone(sc.search_cache("new", EXACT, now=T0 + 1))

    def test_grey_zone_miss_without_verifier(self):
        self.configure(verify_threshold=0.90)
        sc.store_cache("cached", "a", GREY, now=T0)
        self.assertIsNone(sc.search_cache("new", EXACT, now=T0 + 1))

    def test_below_verify_threshold_never_reaches_verifier(self):
        sc.verifier = FakeVerifier({"cached": 1.0})
        sc.store_cache("cached", "a", FAR, now=T0)
        self.assertIsNone(sc.search_cache("new", EXACT, now=T0 + 1))
        self.assertEqual(sc.verifier.calls, [])

    def test_verifier_picks_its_most_confident_candidate(self):
        sc.verifier = FakeVerifier({"first": 0.6, "second": 0.9})
        sc.store_cache("first", "answer 1", GREY, now=T0)
        sc.store_cache("second", "answer 2", [0.84, 0.54, 0.0, 0.0], now=T0)
        hit = sc.search_cache("new", EXACT, now=T0 + 1)
        self.assertEqual(hit["response"], "answer 2")
        self.assertEqual(len(sc.verifier.calls), 1)   # one batch for all candidates

    def test_rule_check_overrides_direct_hit(self):
        sc.store_cache("how do I enable 2FA", "a", EXACT, now=T0)
        self.assertIsNone(sc.search_cache("how do I disable 2FA", EXACT, now=T0 + 1))

    def test_rule_check_falls_through_to_next_candidate(self):
        sc.verifier = FakeVerifier({"how do I turn on the VPN client": 0.9})
        sc.store_cache("how do I turn off the VPN client", "off answer", EXACT, now=T0)
        sc.store_cache("how do I turn on the VPN client", "on answer", GREY, now=T0)
        hit = sc.search_cache("how do I turn on the VPN client", EXACT, now=T0 + 1)
        self.assertEqual(hit["response"], "on answer")


class CacheSkipTest(unittest.TestCase):

    def setUp(self):
        self._saved = (sc.embedding_model, sc._cfg)
        sc.embedding_model = FakeEmbeddingModel()

    def tearDown(self):
        sc.embedding_model, sc._cfg = self._saved

    def test_normal_prompt_uses_cache(self):
        self.assertIsNone(sc.cache_skip_reason("how do I reset my password"))

    def test_time_sensitive_prompt_skips_cache(self):
        self.assertIn("time-sensitive", sc.cache_skip_reason("what is for lunch today"))

    def test_time_sensitive_check_can_be_turned_off(self):
        import dataclasses
        sc._cfg = dataclasses.replace(sc._cfg, skip_time_sensitive=False)
        self.assertIsNone(sc.cache_skip_reason("what is for lunch today"))

    def test_prompt_over_token_limit_skips_cache(self):
        self.assertIn("too long", sc.cache_skip_reason("word " * 11))
        self.assertIsNone(sc.cache_skip_reason("word " * 10))


class MatchingConfigTest(unittest.TestCase):

    def test_verify_threshold_above_similarity_threshold_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.8",
                               "GW_SEMANTIC_CACHE_VERIFY_THRESHOLD": "0.9"})

    def test_equal_thresholds_turn_verifier_off(self):
        cfg = load_settings(env={"GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD": "0.9",
                                 "GW_SEMANTIC_CACHE_VERIFY_THRESHOLD": "0.9"}).semantic_cache
        self.assertFalse(cfg.verification_enabled)


if __name__ == "__main__":
    unittest.main()
