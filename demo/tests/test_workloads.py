"""Offline workload checks: standard library only, no Locust, models or servers.

    python -m unittest discover -s demo/tests -p test_workloads.py -v
"""
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "locust"))
from workloads import (  # noqa: E402
    COLD_PROMPT_SPACE, FAQ_PAIRS, choose_request, fresh_prompt,
    response_error, response_outcome, scenario_name,
)

# These are pure rule functions; importing them loads no ML models or clients.
from smart_ai_gateway.services.cache_checks import conflict, time_sensitive  # noqa: E402


class WorkloadTests(unittest.TestCase):
    def test_cache_mode_contains_the_three_documented_inputs(self):
        rng = random.Random(42)
        cases = [choose_request("cache", rng, i) for i in range(1000)]
        counts = {kind: sum(c.kind == kind for c in cases) for kind in ("faq", "paraphrase", "cold")}
        self.assertTrue(540 < counts["faq"] < 660, counts)
        self.assertTrue(140 < counts["paraphrase"] < 260, counts)
        self.assertTrue(140 < counts["cold"] < 260, counts)
        self.assertEqual({c.endpoint for c in cases}, {"/chat"})
        for case in cases:
            if case.kind == "faq":
                self.assertIn(case.prompt, {pair[0] for pair in FAQ_PAIRS})
            elif case.kind == "paraphrase":
                self.assertIn(case.prompt, {pair[1] for pair in FAQ_PAIRS})

    def test_cold_questions_do_not_repeat_and_rules_reject_number_changes(self):
        prompts = [fresh_prompt(i) for i in range(10000)]
        self.assertEqual(len(set(prompts)), len(prompts))
        for sequence in (0, 1, 8999, 9000, COLD_PROMPT_SPACE - 1):
            with self.subTest(sequence=sequence):
                first, second = fresh_prompt(sequence), fresh_prompt(sequence + 1)
                self.assertIsNotNone(conflict(first, second))
                self.assertIsNone(time_sensitive(first))
                self.assertLess(len(first), 300)

    def test_faqs_have_no_time_words_or_rule_conflicts(self):
        for original, paraphrase in FAQ_PAIRS:
            with self.subTest(original=original):
                self.assertIsNone(time_sensitive(original))
                self.assertIsNone(time_sensitive(paraphrase))
                self.assertIsNone(conflict(original, paraphrase))
                self.assertLess(max(len(original), len(paraphrase)), 300)

    def test_scenario_selects_endpoint_and_preserves_user_identity(self):
        for scenario, endpoint, kind in (("misses", "/chat", "cold"),
                                         ("scheduler", "/scheduler/chat", "scheduler")):
            case = choose_request(scenario, random.Random(1), 17)
            self.assertEqual(case.endpoint, endpoint)
            self.assertEqual(case.kind, kind)
            self.assertEqual(case.payload("demo-user"), {"user_id": "demo-user", "prompt": case.prompt})

    def test_unknown_scenario_is_an_error(self):
        self.assertEqual(scenario_name(" Cache "), "cache")
        with self.assertRaisesRegex(ValueError, "DEMO_SCENARIO"):
            choose_request("typo", random.Random(1), 1)

    def test_failure_status_or_bad_body_cannot_be_counted_as_success(self):
        good = {"response": "answer", "source": "mock_llm"}
        for status, body in ((0, None), (503, good), (200, None), (200, []),
                             (200, {}), (200, {**good, "response": " "}),
                             (200, {**good, "source": "unknown"})):
            with self.subTest(status=status, body=body):
                self.assertIsNotNone(response_error(status, body, "/chat"))
        for source in ("semantic_cache", "gemini", "mock_llm"):
            self.assertIsNone(response_error(200, {**good, "source": source}, "/chat"))
        self.assertIsNone(response_error(200, {**good, "source": "mock"}, "/scheduler/chat"))

    def test_response_outcome_comes_from_the_answer_not_the_input_mix(self):
        self.assertEqual(response_outcome({"source": "semantic_cache"}, "/chat"), "semantic_cache")
        self.assertEqual(response_outcome({"source": "mock_llm"}, "/chat"), "llm")
        self.assertEqual(response_outcome({"source": "gemini", "coalesced": True}, "/chat"), "coalesced")
        self.assertEqual(response_outcome({"source": "mock"}, "/scheduler/chat"), "scheduler")


if __name__ == "__main__":
    unittest.main()
