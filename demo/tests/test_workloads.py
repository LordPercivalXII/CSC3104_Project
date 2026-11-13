"""Offline workload checks: standard library only, no Locust, models or servers.

    python -m unittest discover -s demo/tests -p test_workloads.py -v
"""
from pathlib import Path
import hashlib
import json
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "locust"))
from workloads import (  # noqa: E402
    COLD_PROMPT_SPACE, EvaluationWorkload, choose_request, fresh_prompt, load_dataset,
    response_error, response_outcome, scenario_name,
)

# These are pure rule functions; importing them loads no ML models or clients.
from smart_ai_gateway.services.cache_checks import conflict, time_sensitive  # noqa: E402


class WorkloadTests(unittest.TestCase):
    def test_loads_the_evaluation_file_instead_of_a_duplicate_corpus(self):
        path = Path(__file__).resolve().parents[2] / "eval/cost/data/intents.json"
        dataset = load_dataset(path)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(dataset.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual([(i.intent_id, i.canonical, i.paraphrases) for i in dataset.intents],
                         [(i["id"], i["canonical"], tuple(i["paraphrases"])) for i in doc["intents"]])
        self.assertEqual(dataset.personal_templates, tuple(doc["personal_templates"]))
        self.assertEqual(dataset.time_sensitive, tuple(doc["time_sensitive"]))

    def test_cache_mode_samples_the_documented_eval_categories_and_wordings(self):
        workload = EvaluationWorkload(seed=42, run_id="test")
        cases = [choose_request("cache", random.Random(1), i, workload) for i in range(10000)]
        counts = {kind: sum(c.kind == kind for c in cases) for kind in ("faq", "paraphrase", "personal", "time")}
        self.assertTrue(4300 < counts["faq"] < 4900, counts)
        self.assertTrue(4300 < counts["paraphrase"] < 4900, counts)
        self.assertTrue(400 < counts["personal"] < 600, counts)
        self.assertTrue(220 < counts["time"] < 380, counts)
        self.assertEqual({c.endpoint for c in cases}, {"/chat"})
        by_id = {intent.intent_id: intent for intent in workload.dataset.intents}
        for case in cases:
            if case.kind == "faq":
                self.assertEqual(case.prompt, by_id[case.intent_id].canonical)
            elif case.kind == "paraphrase":
                self.assertIn(case.prompt, by_id[case.intent_id].paraphrases)
            elif case.kind == "time":
                self.assertIn(case.prompt, workload.dataset.time_sensitive)
            else:
                self.assertNotIn("{pasted}", case.prompt)
                self.assertNotIn("{id}", case.prompt)
        ids = [c.intent_id for c in cases if c.kind in ("personal", "time")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sampling_is_repeatable_with_explicit_seed_and_run_id(self):
        first, second = [EvaluationWorkload(seed=9, run_id="fixed") for _ in range(2)]
        self.assertEqual([first.choose(i) for i in range(100)], [second.choose(i) for i in range(100)])

    def test_cache_payload_contains_only_the_gateway_request_fields(self):
        case = EvaluationWorkload(seed=5).choose(0)
        self.assertEqual(case.payload("demo-user"), {"user_id": "demo-user", "prompt": case.prompt})

    def test_environment_uses_the_mounted_file_and_sampling_settings(self):
        fixture = Path(__file__).resolve().parents[2] / "eval/cost/fixtures/intents.json"
        with patch.dict("os.environ", {"DEMO_INTENTS_PATH": str(fixture), "DEMO_DATA_SEED": "17",
                                      "DEMO_PERSONAL_SHARE": "0", "DEMO_TIME_SHARE": "1",
                                      "DEMO_PARAPHRASE_SHARE": "0", "DEMO_ZIPF_S": "0"}):
            workload = EvaluationWorkload.from_environment()
        self.assertEqual(workload.dataset.path, fixture)
        self.assertTrue(all(workload.choose(i).kind == "time" for i in range(30)))

    def test_all_faq_intents_and_their_alternatives_can_be_exercised(self):
        workload = EvaluationWorkload(seed=3, personal_share=0, time_share=0, paraphrase_share=1, zipf_s=0)
        cases = [workload.choose(i) for i in range(5000)]
        self.assertEqual({c.intent_id for c in cases}, {i.intent_id for i in workload.dataset.intents})
        self.assertTrue(all(c.kind == "paraphrase" for c in cases))

    def test_invalid_dataset_and_policy_fail_before_any_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "intents.json"
            with self.assertRaisesRegex(ValueError, "DEMO_INTENTS_PATH"):
                load_dataset(path)
            path.write_text('{"intents":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid evaluation dataset"):
                load_dataset(path)
        for options in ({"personal_share": 0.9, "time_share": 0.5}, {"paraphrase_share": -1},
                        {"time_share": float("nan")}, {"zipf_s": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EvaluationWorkload(**options)

    def test_cold_questions_do_not_repeat_and_rules_reject_number_changes(self):
        prompts = [fresh_prompt(i) for i in range(10000)]
        self.assertEqual(len(set(prompts)), len(prompts))
        for sequence in (0, 1, 8999, 9000, COLD_PROMPT_SPACE - 1):
            with self.subTest(sequence=sequence):
                first, second = fresh_prompt(sequence), fresh_prompt(sequence + 1)
                self.assertIsNotNone(conflict(first, second))
                self.assertIsNone(time_sensitive(first))
                self.assertLess(len(first), 300)

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
        with self.assertRaisesRegex(ValueError, "EvaluationWorkload"):
            choose_request("cache", random.Random(1), 1)

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
