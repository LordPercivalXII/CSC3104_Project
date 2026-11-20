"""Connection recycling: keep-alive clients are asked to reconnect after max_age_seconds."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                                       # noqa: E402
from fastapi.testclient import TestClient                                         # noqa: E402

from smart_ai_gateway.connection_recycling import (                               # noqa: E402
    ConnectionRecyclingMiddleware, max_age_from_env,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_client(max_age, clock):
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(ConnectionRecyclingMiddleware, max_age_seconds=max_age, clock=clock)
    return TestClient(app)


class ConnectionRecyclingTest(unittest.TestCase):
    def test_young_connection_is_kept_open(self):
        clock = FakeClock()
        client = make_client(30, clock)
        response = client.get("/ping")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers.get("connection"), "close")

    def test_old_connection_is_asked_to_close_once(self):
        clock = FakeClock()
        client = make_client(30, clock)
        client.get("/ping")                       # first seen at t=1000
        clock.now += 31
        closing = client.get("/ping")
        self.assertEqual(closing.status_code, 200)
        self.assertEqual(closing.json(), {"ok": True})
        self.assertEqual(closing.headers.get("connection"), "close")
        # The same address now counts as a new connection
        self.assertNotEqual(client.get("/ping").headers.get("connection"), "close")

    def test_zero_turns_it_off(self):
        clock = FakeClock()
        client = make_client(0, clock)
        client.get("/ping")
        clock.now += 10_000
        self.assertNotEqual(client.get("/ping").headers.get("connection"), "close")

    def test_forgotten_connections_are_pruned(self):
        clock = FakeClock()
        app = FastAPI()
        middleware = ConnectionRecyclingMiddleware(app, max_age_seconds=30, clock=clock)
        middleware.first_seen = {(("10.0.0.1", port), ("srv", 8000)): 0.0 for port in range(500)}
        client = TestClient(middleware)
        client.get("/missing")                    # any request triggers the periodic prune
        self.assertEqual(len(middleware.first_seen), 1)   # only the current connection is left

    def test_env_setting(self):
        with mock.patch.dict(os.environ, {"CONNECTION_MAX_AGE_SECONDS": "12"}):
            self.assertEqual(max_age_from_env(), 12.0)
        with mock.patch.dict(os.environ, {"CONNECTION_MAX_AGE_SECONDS": "-1"}):
            with self.assertRaises(ValueError):
                max_age_from_env()



class StdoutLogTest(unittest.TestCase):
    """A log path of "-" writes JSON lines to stdout (Kubernetes keeps them; files in a pod are lost)."""

    def test_dash_means_stdout(self):
        import io
        import json
        from contextlib import redirect_stdout
        from smart_ai_gateway.config import resolve_path
        from smart_ai_gateway.services.semantic_cache import append_jsonl

        self.assertEqual(str(resolve_path("-")), "-")
        out = io.StringIO()
        with redirect_stdout(out):
            append_jsonl(resolve_path("-"), {"stage": "direct_hit", "prompt_chars": 12})
        self.assertEqual(json.loads(out.getvalue().strip()), {"stage": "direct_hit", "prompt_chars": 12})


if __name__ == "__main__":
    unittest.main()
