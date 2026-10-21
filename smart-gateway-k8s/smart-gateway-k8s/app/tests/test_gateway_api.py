"""HTTP-level tests for the unified app, run in its default config (no caches, no queue)."""
import unittest

from fastapi.testclient import TestClient

import main


class TestGatewayApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Entering the context runs the lifespan, which starts the scheduler worker.
        cls._ctx = TestClient(main.app)
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_health(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["cache_mode"], "none")

    def test_frontend_is_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/script.js").status_code, 200)

    def test_chat_with_prompt_only_like_the_web_page_sends(self):
        body = self.client.post("/chat", json={"prompt": "hi"}).json()
        self.assertEqual(body["response"], "You asked: hi")
        self.assertEqual(body["source"], "mock_llm")

    def test_chat_accepts_user_and_team(self):
        r = self.client.post("/chat", json={"prompt": "hi", "user_id": "alice", "team_id": "backend"})
        self.assertEqual(r.status_code, 200)

    def test_chat_rejects_blank_prompt(self):
        self.assertEqual(self.client.post("/chat", json={"prompt": "   "}).status_code, 422)

    def test_cache_stats_when_disabled(self):
        self.assertEqual(self.client.get("/cache/stats").json(), {"exact_cache_active": False})

    def test_scheduler_chat_still_works(self):
        body = self.client.post("/scheduler/chat", json={"user_id": "A", "prompt": "A1"}).json()
        self.assertEqual(body["user_id"], "A")
        self.assertEqual(body["response"], "You asked: A1")


if __name__ == "__main__":
    unittest.main()
