"""
Tests for LLMAnswer: usage parsing, cacheable rules, and how the scheduler passes them on.
No API key, network or quota needed.

    python -m pytest tests/test_llm_usage.py -v
"""
import dataclasses
import json
import os
import sys
import threading
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from scheduler.scheduler_worker import SchedulerWorker      # noqa: E402
from smart_ai_gateway.services import llm                    # noqa: E402

RESULT_TIMEOUT = 5


def response(text, finish="STOP", usage="default"):
    if usage == "default":
        usage = types.SimpleNamespace(
            prompt_token_count=10, candidates_token_count=20,
            thoughts_token_count=5, total_token_count=35)
    return types.SimpleNamespace(
        text=text, prompt_feedback=None, usage_metadata=usage,
        candidates=[types.SimpleNamespace(finish_reason=finish)])


class GeminiAnswerTest(unittest.TestCase):

    def setUp(self):
        self._saved = (llm.client, llm._cfg)
        llm._cfg = dataclasses.replace(llm._cfg, provider="gemini", model="gemini-test")

    def tearDown(self):
        llm.client, llm._cfg = self._saved

    def ask(self, resp):
        models = types.SimpleNamespace(generate_content=lambda model, contents, config: resp)
        llm.client = types.SimpleNamespace(models=models)
        return llm.generate_answer("q")

    def test_usage_parsed(self):
        answer = self.ask(response("hi"))
        self.assertEqual(answer.usage, {
            "prompt_tokens": 10, "output_tokens": 20, "thinking_tokens": 5,
            "total_tokens": 35, "model": "gemini:gemini-test", "estimated": False})

    def test_none_counts_are_zero(self):
        usage = types.SimpleNamespace(prompt_token_count=3, candidates_token_count=None,
                                      thoughts_token_count=None, total_token_count=None)
        answer = self.ask(response("hi", usage=usage))
        self.assertEqual(answer.usage["prompt_tokens"], 3)
        self.assertEqual(answer.usage["output_tokens"], 0)
        self.assertEqual(answer.usage["thinking_tokens"], 0)
        self.assertEqual(answer.usage["total_tokens"], 0)

    def test_missing_usage_metadata_is_zero(self):
        self.assertEqual(self.ask(response("hi", usage=None)).usage["total_tokens"], 0)

    def test_stop_is_cacheable(self):
        answer = self.ask(response("hi", "STOP"))
        self.assertTrue(answer.cacheable)
        self.assertEqual(answer.finish_reason, "STOP")

    def test_enum_finish_reason_becomes_name(self):
        from google.genai import types as gtypes
        answer = self.ask(response("hi", gtypes.FinishReason.STOP))
        self.assertEqual(answer.finish_reason, "STOP")
        self.assertTrue(answer.cacheable)

    def test_max_tokens_returned_but_not_cacheable(self):
        answer = self.ask(response("cut off", "MAX_TOKENS"))
        self.assertEqual(answer.text, "cut off")
        self.assertFalse(answer.cacheable)
        self.assertEqual(answer.finish_reason, "MAX_TOKENS")

    def test_safety_with_text_not_cacheable(self):
        self.assertFalse(self.ask(response("partial", "SAFETY")).cacheable)

    def test_empty_answer_still_unavailable(self):
        with self.assertRaises(llm.LLMUnavailable):
            self.ask(response("", "MAX_TOKENS"))


class MockAnswerTest(unittest.TestCase):

    def test_mock_usage_estimated(self):
        saved = llm.client
        llm.client = None
        try:
            answer = llm.generate_answer("abcdefgh")
        finally:
            llm.client = saved
        text = llm._mock_template.format(prompt="abcdefgh")
        self.assertTrue(answer.cacheable)
        self.assertEqual(answer.finish_reason, "STOP")
        self.assertEqual(answer.usage["prompt_tokens"], 2)
        self.assertEqual(answer.usage["output_tokens"], -(-len(text) // 4))
        self.assertEqual(answer.usage["thinking_tokens"], 0)
        self.assertEqual(answer.usage["model"], "mock")
        self.assertTrue(answer.usage["estimated"])


class SchedulerPassThroughTest(unittest.TestCase):

    def run_one(self, fn):
        worker = SchedulerWorker(inference_fn=fn)
        worker.running = True
        future = worker.submit("u1", "q", dedup=True)
        worker.running = False
        worker.start()
        try:
            return [future.result(timeout=RESULT_TIMEOUT)]
        finally:
            worker.stop()

    def test_llm_answer_fields(self):
        usage = {"prompt_tokens": 1, "output_tokens": 2, "thinking_tokens": 0,
                 "total_tokens": 3, "model": "m", "estimated": False}
        fn = lambda p: llm.LLMAnswer("a", False, "MAX_TOKENS", usage)
        result = self.run_one(fn)[0]
        self.assertEqual(result["response"], "a")
        self.assertFalse(result["cacheable"])
        self.assertEqual(result["usage"], usage)
        self.assertEqual(result["finish_reason"], "MAX_TOKENS")
        json.dumps(result)

    def test_string_fn_defaults(self):
        result = self.run_one(lambda p: "plain")[0]
        self.assertEqual(result["response"], "plain")
        self.assertTrue(result["cacheable"])
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["finish_reason"])
        json.dumps(result)

    def test_coalesced_follower_gets_same_fields(self):
        gate = threading.Event()
        usage = {"prompt_tokens": 1, "output_tokens": 2, "thinking_tokens": 0,
                 "total_tokens": 3, "model": "m", "estimated": False}

        def fn(prompt):
            gate.wait(RESULT_TIMEOUT)
            return llm.LLMAnswer("a", False, "MAX_TOKENS", usage)

        worker = SchedulerWorker(inference_fn=fn)
        worker.running = True
        leader = worker.submit("u1", "same", dedup=True)
        follower = worker.submit("u2", "same", dedup=True)
        worker.running = False
        worker.start()
        try:
            gate.set()
            results = [leader.result(timeout=RESULT_TIMEOUT), follower.result(timeout=RESULT_TIMEOUT)]
        finally:
            gate.set()
            worker.stop()
        self.assertTrue(any(r.get("coalesced") for r in results))
        for r in results:
            self.assertFalse(r["cacheable"])
            self.assertEqual(r["usage"], usage)
            self.assertEqual(r["finish_reason"], "MAX_TOKENS")
            json.dumps(r)


if __name__ == "__main__":
    unittest.main()
