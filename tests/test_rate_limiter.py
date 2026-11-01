"""
Tests for the LLM rate limiter, alone and connected to the scheduler.
Uses the mock LLM, so no API key is needed.

    python -m pytest tests/test_rate_limiter.py -v
    REDIS_TEST_URL=redis://localhost:6379/15 python -m pytest tests/test_rate_limiter.py -v
"""
import os
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import scheduler_worker                                   # noqa: E402
from scheduler.factory import create_scheduler_worker                    # noqa: E402
from scheduler.rate_limiter import MemoryRateLimiter                     # noqa: E402
from scheduler.redis_backend import (                                    # noqa: E402
    RedisFairScheduler, RedisJobStore, RedisRateLimiter,
)
from scheduler.scheduler_worker import SchedulerWorker                   # noqa: E402

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")

# Short windows keep the tests fast
WINDOW = 1


class StartTimes:
    """Inference function that records when each LLM call starts."""

    def __init__(self):
        self.times = []
        self.users = []
        self.lock = threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.times.append(time.monotonic())
            self.users.append(prompt[0])
        return f"You asked: {prompt}"

    def max_in_any_window(self):
        # Calls start a few ms after their slot is granted, so measure
        # with a slightly shorter window to avoid counting that jitter
        window = WINDOW * 0.9
        return max(
            sum(1 for t in self.times if start <= t < start + window)
            for start in self.times
        )


class TestMemoryRateLimiter(unittest.TestCase):

    def test_allows_up_to_limit_then_says_how_long_to_wait(self):
        limiter = MemoryRateLimiter(limit=3, window_seconds=WINDOW)

        self.assertEqual([limiter.try_acquire() for _ in range(3)], [0, 0, 0])

        wait = limiter.try_acquire()
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, WINDOW)

    def test_slots_free_up_after_the_window(self):
        limiter = MemoryRateLimiter(limit=1, window_seconds=0.2)
        limiter.try_acquire()
        self.assertGreater(limiter.try_acquire(), 0)

        time.sleep(0.25)
        self.assertEqual(limiter.try_acquire(), 0)

    def test_release_gives_the_slot_back(self):
        limiter = MemoryRateLimiter(limit=1, window_seconds=WINDOW)
        limiter.try_acquire()
        limiter.release()
        self.assertEqual(limiter.try_acquire(), 0)


class RedisTestCase(unittest.TestCase):

    def setUp(self):
        self.prefix = f"test:{uuid.uuid4().hex[:8]}:"

        if REDIS_TEST_URL:
            import redis
            self.make_client = lambda: redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
        else:
            import fakeredis
            server = fakeredis.FakeServer()
            self.make_client = lambda: fakeredis.FakeRedis(server=server, decode_responses=True)

        self.redis = self.make_client()
        self.addCleanup(self.delete_test_keys)

    def delete_test_keys(self):
        keys = list(self.redis.scan_iter(match=f"{self.prefix}*"))
        if keys:
            self.redis.delete(*keys)

    def make_limiter(self, limit, window=WINDOW):
        return RedisRateLimiter(self.make_client(), limit, window, prefix=self.prefix)


class TestRedisRateLimiter(RedisTestCase):

    def test_allows_up_to_limit_then_says_how_long_to_wait(self):
        limiter = self.make_limiter(3)

        self.assertEqual([limiter.try_acquire() for _ in range(3)], [0, 0, 0])

        wait = limiter.try_acquire()
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, WINDOW)

    def test_pods_share_one_limit(self):
        pod1, pod2 = self.make_limiter(3), self.make_limiter(3)

        pod1.try_acquire()
        pod1.try_acquire()
        self.assertEqual(pod2.try_acquire(), 0)
        self.assertGreater(pod2.try_acquire(), 0)
        self.assertGreater(pod1.try_acquire(), 0)

    def test_slots_free_up_after_the_window(self):
        limiter = self.make_limiter(1, window=0.2)
        limiter.try_acquire()
        self.assertGreater(limiter.try_acquire(), 0)

        time.sleep(0.25)
        self.assertEqual(limiter.try_acquire(), 0)

    def test_release_gives_the_slot_back(self):
        pod1, pod2 = self.make_limiter(1), self.make_limiter(1)
        pod1.try_acquire()
        pod1.release()
        self.assertEqual(pod2.try_acquire(), 0)

    def test_key_expires_when_idle(self):
        self.make_limiter(1).try_acquire()
        self.assertGreater(self.redis.pttl(f"{self.prefix}ratelimit"), 0)


class TestWorkerWithRateLimit(RedisTestCase):

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(scheduler_worker, "print", create=True))

    def make_pod(self, llm, limiter, num_workers=1):
        client = self.make_client()
        pod = SchedulerWorker(
            inference_fn=llm,
            scheduler=RedisFairScheduler(client, self.prefix),
            job_store=RedisJobStore(client, self.prefix),
            num_workers=num_workers,
            rate_limiter=limiter,
        )
        self.addCleanup(pod.stop)
        return pod

    def test_limit_holds_across_pods(self):
        # 2 pods with 2 workers each could start 4 calls at once,
        # but the shared limit allows only 2 per window
        llm = StartTimes()
        pods = [self.make_pod(llm, self.make_limiter(2), num_workers=2) for _ in range(2)]
        for pod in pods:
            pod.start()

        futures = [pods[i % 2].submit("A", f"A{i}") for i in range(6)]
        for future in futures:
            future.result(timeout=10)

        self.assertEqual(len(llm.times), 6)
        self.assertLessEqual(llm.max_in_any_window(), 2)

    def test_queue_stays_fair_while_rate_limited(self):
        llm = StartTimes()
        pod = self.make_pod(llm, self.make_limiter(2))

        pod.running = True
        futures = [pod.submit("A", f"A{i}") for i in range(4)]
        futures.append(pod.submit("B", "B1"))
        pod.running = False

        pod.start()
        for future in futures:
            future.result(timeout=10)

        # B still gets the second turn even though everyone was waiting on the limit
        self.assertEqual(llm.users, ["A", "B", "A", "A", "A"])

    def test_skipped_request_gives_its_slot_back(self):
        # A cancelled request is skipped without an LLM call, so the next
        # request must not wait for the window to pass
        llm = StartTimes()
        pod = self.make_pod(llm, self.make_limiter(1, window=5))

        pod.running = True
        pod.submit("A", "A1").cancel()
        future = pod.submit("B", "B1")
        pod.running = False

        start = time.monotonic()
        pod.start()
        future.result(timeout=10)

        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(llm.users, ["B"])

    def test_memory_mode_limit(self):
        llm = StartTimes()
        pod = SchedulerWorker(inference_fn=llm, rate_limiter=MemoryRateLimiter(2, WINDOW))
        self.addCleanup(pod.stop)
        pod.start()

        futures = [pod.submit("A", f"A{i}") for i in range(4)]
        for future in futures:
            future.result(timeout=10)

        self.assertLessEqual(llm.max_in_any_window(), 2)


class TestFactoryRateLimit(unittest.TestCase):

    def test_no_limit_by_default(self):
        with mock.patch.dict(os.environ, {"SCHEDULER_BACKEND": "memory"}, clear=False):
            os.environ.pop("LLM_RATE_LIMIT_PER_MINUTE", None)
            self.assertIsNone(create_scheduler_worker().rate_limiter)

    def test_limit_from_env(self):
        env = {"SCHEDULER_BACKEND": "memory", "LLM_RATE_LIMIT_PER_MINUTE": "20"}
        with mock.patch.dict(os.environ, env):
            limiter = create_scheduler_worker().rate_limiter

        self.assertIsInstance(limiter, MemoryRateLimiter)
        self.assertEqual(limiter.limit, 20)
        self.assertEqual(limiter.window_seconds, 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)