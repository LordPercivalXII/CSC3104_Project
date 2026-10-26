"""
LLM tests: the Gemini call, its error handling, and the mock provider.

Uses a stand-in for the Gemini client, so no API key or network is needed and no quota is
used. Needs google-genai installed.

Run from the tests folder:
    python test_llm.py
"""
import dataclasses
import os
import sys
import types
import unittest

# Makes "smart_ai_gateway" importable when running from inside tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# llm creates its Gemini client on import; a dummy key lets it start (the client is replaced below)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from google.genai import errors                               # noqa: E402

from smart_ai_gateway.config import ConfigError, load_settings  # noqa: E402
from smart_ai_gateway.services import llm                       # noqa: E402


def api_error(cls, code, status, message):
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return self.response


def fake_response(text, block_reason=None, finish_reason="STOP"):
    return types.SimpleNamespace(
        text=text,
        prompt_feedback=types.SimpleNamespace(block_reason=block_reason) if block_reason else None,
        candidates=[types.SimpleNamespace(finish_reason=finish_reason)],
    )


class GeminiTest(unittest.TestCase):

    def setUp(self):
        self._saved = (llm.client, llm._cfg)
        llm._cfg = dataclasses.replace(llm._cfg, provider="gemini", model="gemini-test", thinking_level="low")

    def tearDown(self):
        llm.client, llm._cfg = self._saved

    def use(self, models):
        llm.client = types.SimpleNamespace(models=models)
        return models

    def test_answer_is_returned_trimmed(self):
        self.use(FakeModels(fake_response("  Restart the router.  \n")))
        self.assertEqual(llm.generate_answer("wifi down"), "Restart the router.")

    def test_request_uses_settings(self):
        models = self.use(FakeModels(fake_response("ok")))
        llm.generate_answer("hello")
        call = models.calls[0]
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["contents"], "hello")
        self.assertEqual(call["config"].system_instruction, llm._cfg.system_instruction)
        self.assertEqual(call["config"].max_output_tokens, llm._cfg.max_output_tokens)
        self.assertEqual(call["config"].thinking_config.thinking_level.value, "LOW")

    def test_empty_thinking_level_uses_model_default(self):
        llm._cfg = dataclasses.replace(llm._cfg, thinking_level="")
        models = self.use(FakeModels(fake_response("ok")))
        llm.generate_answer("hello")
        self.assertIsNone(models.calls[0]["config"].thinking_config)

    def test_rate_limit_is_marked(self):
        self.use(FakeModels(error=api_error(errors.ClientError, 429, "RESOURCE_EXHAUSTED", "quota exceeded")))
        with self.assertRaises(llm.LLMUnavailable) as caught:
            llm.generate_answer("hello")
        self.assertTrue(caught.exception.rate_limited)
        self.assertIn("rate limit", str(caught.exception))

    def test_server_error_is_unavailable(self):
        self.use(FakeModels(error=api_error(errors.ServerError, 503, "UNAVAILABLE", "overloaded")))
        with self.assertRaises(llm.LLMUnavailable) as caught:
            llm.generate_answer("hello")
        self.assertFalse(caught.exception.rate_limited)
        self.assertIn("503", str(caught.exception))

    def test_bad_key_is_unavailable(self):
        self.use(FakeModels(error=api_error(errors.ClientError, 400, "INVALID_ARGUMENT", "API key not valid")))
        with self.assertRaises(llm.LLMUnavailable) as caught:
            llm.generate_answer("hello")
        self.assertIn("API key not valid", str(caught.exception))

    def test_network_failure_is_unavailable(self):
        self.use(FakeModels(error=TimeoutError("timed out")))
        with self.assertRaises(llm.LLMUnavailable):
            llm.generate_answer("hello")

    def test_blocked_prompt_is_unavailable(self):
        self.use(FakeModels(fake_response(None, block_reason="SAFETY")))
        with self.assertRaises(llm.LLMUnavailable) as caught:
            llm.generate_answer("hello")
        self.assertIn("blocked", str(caught.exception))

    def test_empty_answer_is_unavailable(self):
        self.use(FakeModels(fake_response("", finish_reason="MAX_TOKENS")))
        with self.assertRaises(llm.LLMUnavailable) as caught:
            llm.generate_answer("hello")
        self.assertIn("MAX_TOKENS", str(caught.exception))

    def test_source_name(self):
        self.use(FakeModels())
        self.assertEqual(llm.source_name(), "gemini")


class MockAndStartupTest(unittest.TestCase):

    def setUp(self):
        self._saved = (llm.client, llm._cfg, os.environ.get("GEMINI_API_KEY"), os.environ.get("GOOGLE_API_KEY"))

    def tearDown(self):
        llm.client, llm._cfg = self._saved[:2]
        for name, value in zip(("GEMINI_API_KEY", "GOOGLE_API_KEY"), self._saved[2:]):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_mock_provider_uses_template(self):
        llm.client = None
        self.assertEqual(llm.generate_answer("hi"), llm._mock_template.format(prompt="hi"))
        self.assertEqual(llm.source_name(), "mock_llm")

    def test_missing_key_stops_startup_with_clear_message(self):
        llm._cfg = dataclasses.replace(llm._cfg, provider="gemini")
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        with self.assertRaises(RuntimeError) as caught:
            llm._create_client()
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_mock_provider_needs_no_key(self):
        llm._cfg = dataclasses.replace(llm._cfg, provider="mock")
        os.environ.pop("GEMINI_API_KEY", None)
        self.assertIsNone(llm._create_client())


class LLMConfigTest(unittest.TestCase):

    def test_answered_by_tags(self):
        cfg = load_settings(env={"GW_LLM_PROVIDER": "gemini", "GW_LLM_MODEL": "gemini-3.8-flash"}).llm
        self.assertEqual(cfg.answered_by, "gemini:gemini-3.8-flash")
        self.assertEqual(load_settings(env={"GW_LLM_PROVIDER": "mock"}).llm.answered_by, "mock")

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"GW_LLM_PROVIDER": "openai"})

    def test_unknown_thinking_level_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(env={"GW_LLM_THINKING_LEVEL": "extreme"})


if __name__ == "__main__":
    unittest.main()
