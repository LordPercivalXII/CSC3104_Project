"""
Tests for request coalescing: identical questions asked at the same time
share one LLM call. Uses the mock LLM, so no API key is needed.

    python -m pytest tests/test_coalescing.py -v
    REDIS_TEST_URL=redis://localhost:6379/15 python -m pytest tests/test_coalescing.py -v
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
from scheduler.redis_backend import RedisFairScheduler, RedisJobStore    # noqa: E402
from scheduler.scheduler_worker import SchedulerWorker                   # noqa: E402

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")
RESULT_TIMEOUT = 5


class CountingLLM:
    """Mock LLM that counts its calls."""

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.lock = threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.calls += 1
        if self.fail:
            raise RuntimeError("Simulated LLM processing failure")
        return f"You asked: {prompt}"


def submit_all(pods, requests):
    """Submit (pod index, user, prompt) before any worker starts, so all of
    them are waiting together, then start the pods."""
    for pod in pods:
        pod.running = True
    futures = [pods[i].submit(user, prompt, dedup=True) for i, user, prompt in requests]
    for pod in pods:
        pod.running = False
    for pod in pods:
        pod.start()
    return futures


class RedisPodsTestCase(unittest.TestCase):

    def setUp(self):
        self.prefix = f"test:{uuid.uuid4().hex[:8]}:"
        self.enterContext(mock.patch.object(scheduler_worker, "print", create=True))

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

    def make_pod(self, llm, coalesce=True):
        client = self.make_client()
        pod = SchedulerWorker(
            inference_fn=llm,
            scheduler=RedisFairScheduler(client, self.prefix),
            job_store=RedisJobStore(client, self.prefix),
            coalesce=coalesce,
        )
        self.addCleanup(pod.stop)
        return pod

    def make_pods(self, llm, count=3):
        return [self.make_pod(llm) for _ in range(count)]


class TestCoalescingAcrossPods(RedisPodsTestCase):

    def test_identical_questions_share_one_llm_call(self):
        llm = CountingLLM()
        pods = self.make_pods(llm)
        requests = [(i % 3, f"user{i}", "How do I reset my VPN password?") for i in range(5)]

        results = [f.result(timeout=RESULT_TIMEOUT) for f in submit_all(pods, requests)]

        self.assertEqual(llm.calls, 1)
        self.assertEqual({r["response"] for r in results},
                         {"You asked: How do I reset my VPN password?"})
        # Each caller gets its own IDs back; only the leader made the call
        self.assertEqual([r["user_id"] for r in results], [f"user{i}" for i in range(5)])
        self.assertEqual(len({r["request_id"] for r in results}), 5)
        self.assertEqual([r.get("coalesced", False) for r in results],
                         [False, True, True, True, True])

    def test_case_and_spacing_are_ignored(self):
        llm = CountingLLM()
        pods = self.make_pods(llm, count=2)
        requests = [(0, "A", "What is Docker?"), (1, "B", "  what IS   docker? ")]

        for future in submit_all(pods, requests):
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 1)

    def test_different_questions_are_not_merged(self):
        llm = CountingLLM()
        pods = self.make_pods(llm, count=2)
        requests = [(0, "A", "What is Docker?"), (1, "B", "What is Kubernetes?")]

        for future in submit_all(pods, requests):
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 2)

    def test_without_dedup_every_request_calls_the_llm(self):
        # e.g. time-sensitive questions, which main.py never shares
        llm = CountingLLM()
        pod = self.make_pod(llm)
        pod.running = True
        futures = [pod.submit(f"user{i}", "What is today's date?") for i in range(3)]
        pod.running = False
        pod.start()

        for future in futures:
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 3)

    def test_can_be_turned_off(self):
        llm = CountingLLM()
        pod = self.make_pod(llm, coalesce=False)

        for future in submit_all([pod], [(0, f"user{i}", "What is Docker?") for i in range(3)]):
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 3)

    def test_waiters_still_answered_if_leader_gives_up(self):
        llm = CountingLLM()
        pod_a, pod_b = self.make_pods(llm, count=2)

        pod_a.running = pod_b.running = True
        leader = pod_a.submit("A", "What is Docker?", dedup=True)
        follower = pod_b.submit("B", "What is Docker?", dedup=True)
        leader.cancel()
        pod_a.running = pod_b.running = False
        pod_a.start()
        pod_b.start()

        self.assertEqual(follower.result(timeout=RESULT_TIMEOUT)["response"],
                         "You asked: What is Docker?")
        self.assertEqual(llm.calls, 1)

    def test_follower_caches_answer_when_leader_left(self):
        # The leader's caller left, so nobody would store the paid-for answer; one
        # follower is named the store owner instead.
        llm = CountingLLM()
        pod_a, pod_b = self.make_pods(llm, count=2)
        pod_a.running = pod_b.running = True
        leader = pod_a.submit("A", "What is Docker?", dedup=True)
        follower = pod_b.submit("B", "What is Docker?", dedup=True)
        leader.cancel()
        pod_a.running = pod_b.running = False
        pod_a.start()
        pod_b.start()

        result = follower.result(timeout=RESULT_TIMEOUT)
        self.assertTrue(result.get("coalesced"))
        self.assertTrue(result.get("store_owner"))
        self.assertEqual(llm.calls, 1)

    def test_cancelled_leader_alone_does_not_block_the_question(self):
        llm = CountingLLM()
        pod = self.make_pod(llm)

        pod.running = True
        leader = pod.submit("A", "What is Docker?", dedup=True)
        leader_id = next(iter(pod.jobs))
        leader.cancel()
        retry = pod.submit("B", "What is Docker?", dedup=True)
        pod.running = False
        pod.start()

        result = retry.result(timeout=RESULT_TIMEOUT)
        self.assertFalse(result.get("coalesced", False))
        self.assertEqual(pod.get_status(leader_id), "CANCELLED")
        self.assertEqual(llm.calls, 1)

    def test_failure_reaches_every_waiter(self):
        llm = CountingLLM(fail=True)
        pods = self.make_pods(llm)
        futures = submit_all(pods, [(i, f"user{i}", "What is Docker?") for i in range(3)])
        request_ids = [list(pod.jobs) for pod in pods]

        for future in futures:
            with self.assertRaisesRegex(RuntimeError, "Simulated LLM processing failure"):
                future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 1)
        for pod, ids in zip(pods, request_ids):
            for request_id in ids:
                self.assertEqual(pod.get_status(request_id), "FAILED")

    def test_waiter_status_is_completed(self):
        llm = CountingLLM()
        pod_a, pod_b = self.make_pods(llm, count=2)
        futures = submit_all([pod_a, pod_b], [(0, "A", "What is Docker?"), (1, "B", "What is Docker?")])
        follower_id = next(iter(pod_b.jobs))

        for future in futures:
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(pod_b.get_status(follower_id), "COMPLETED")

    def wait_until_no_question_in_flight(self):
        deadline = time.monotonic() + RESULT_TIMEOUT
        while list(self.redis.scan_iter(match=f"{self.prefix}inflight:*")):
            self.assertLess(time.monotonic(), deadline, "question still marked in flight")
            time.sleep(0.01)

    def test_answered_question_can_be_asked_again(self):
        # After the answer, repeats are the semantic cache's job, not ours
        llm = CountingLLM()
        pod = self.make_pod(llm)
        pod.start()

        pod.submit("A", "What is Docker?", dedup=True).result(timeout=RESULT_TIMEOUT)

        # A's answer is sent before the call is marked finished, so a repeat
        # in that moment still shares it (correct). Wait for the end of the call.
        self.wait_until_no_question_in_flight()
        again = pod.submit("B", "What is Docker?", dedup=True).result(timeout=RESULT_TIMEOUT)

        self.assertEqual(llm.calls, 2)
        self.assertFalse(again.get("coalesced", False))

    def test_no_keys_left_behind(self):
        llm = CountingLLM()
        pods = self.make_pods(llm)
        for future in submit_all(pods, [(i, f"user{i}", "What is Docker?") for i in range(3)]):
            future.result(timeout=RESULT_TIMEOUT)

        leftovers = list(self.redis.scan_iter(match=f"{self.prefix}inflight:*")) + \
            list(self.redis.scan_iter(match=f"{self.prefix}waiters:*"))
        self.assertEqual(leftovers, [])


class TestCoalescingInMemory(unittest.TestCase):

    def setUp(self):
        self.enterContext(mock.patch.object(scheduler_worker, "print", create=True))

    def test_identical_questions_share_one_llm_call(self):
        llm = CountingLLM()
        pod = SchedulerWorker(inference_fn=llm, num_workers=2)
        self.addCleanup(pod.stop)

        results = [f.result(timeout=RESULT_TIMEOUT) for f in
                   submit_all([pod], [(0, f"user{i}", "What is Docker?") for i in range(4)])]

        self.assertEqual(llm.calls, 1)
        self.assertEqual(sum(r.get("coalesced", False) for r in results), 3)

    def test_waiters_still_answered_if_leader_gives_up(self):
        llm = CountingLLM()
        pod = SchedulerWorker(inference_fn=llm)
        self.addCleanup(pod.stop)

        pod.running = True
        leader = pod.submit("A", "What is Docker?", dedup=True)
        follower = pod.submit("B", "What is Docker?", dedup=True)
        leader.cancel()
        pod.running = False
        pod.start()

        follower.result(timeout=RESULT_TIMEOUT)
        self.assertEqual(llm.calls, 1)


class TestFactoryCoalesce(unittest.TestCase):

    def test_on_by_default(self):
        with mock.patch.dict(os.environ, {"SCHEDULER_BACKEND": "memory"}):
            os.environ.pop("SCHEDULER_COALESCE", None)
            self.assertTrue(create_scheduler_worker().coalesce)

    def test_can_be_turned_off(self):
        env = {"SCHEDULER_BACKEND": "memory", "SCHEDULER_COALESCE": "false"}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(create_scheduler_worker().coalesce)


if __name__ == "__main__":
    unittest.main(verbosity=2)