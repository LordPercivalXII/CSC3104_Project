"""
Tests for the Redis-backed fair scheduler, including several "pods"
(SchedulerWorker instances) sharing one Redis.

Uses fakeredis by default, so no Redis server is needed:
    pip install -r requirements-dev.txt
    python -m pytest tests/test_redis_scheduler.py -v

To run against a real Redis instead (uses database 15, only touches its own keys):
    REDIS_TEST_URL=redis://localhost:6379/15 python -m pytest tests/test_redis_scheduler.py -v
"""
import os
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import scheduler_worker                                  # noqa: E402
from scheduler.redis_backend import RedisFairScheduler, RedisJobStore   # noqa: E402
from scheduler.scheduler_worker import SchedulerWorker                  # noqa: E402

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")
RESULT_TIMEOUT = 5


def fast_inference(prompt):
    time.sleep(0.05)
    return f"You asked: {prompt}"


def failing_inference(prompt):
    raise RuntimeError("Simulated LLM processing failure")


class RedisTestCase(unittest.TestCase):
    """Gives each test its own key prefix and a way to make extra clients,
    each standing in for a separate pod connected to the same Redis."""

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

    def make_scheduler(self):
        return RedisFairScheduler(self.make_client(), self.prefix)


class TestRedisFairScheduler(RedisTestCase):

    def setUp(self):
        super().setUp()
        self.scheduler = self.make_scheduler()

    def submit(self, user, count, scheduler=None):
        for i in range(1, count + 1):
            (scheduler or self.scheduler).add_request(user, {"id": f"{user}{i}"})

    def drain(self, scheduler=None):
        order = []
        while (item := (scheduler or self.scheduler).get_next_request()) is not None:
            order.append(item)
        return order

    def test_empty_queue_returns_none(self):
        self.assertIsNone(self.scheduler.get_next_request())
        self.assertEqual(self.scheduler.pending_count(), 0)

    def test_request_round_trips_as_dict(self):
        request = {"request_id": "r1", "prompt": "What is Docker?", "reply_to": "pod-1"}
        self.scheduler.add_request("A", request)
        self.assertEqual(self.scheduler.get_next_request(), ("A", request))

    def test_pending_count_tracks_queue(self):
        self.submit("A", 2)
        self.submit("B", 1)
        self.assertEqual(self.scheduler.pending_count(), 3)

        self.scheduler.get_next_request()
        self.assertEqual(self.scheduler.pending_count(), 2)

    def test_heavy_employee_cannot_starve_light_employee(self):
        self.submit("A", 10)
        self.submit("B", 2)

        users = [user for user, _ in self.drain()]
        self.assertEqual(users, ["A", "B", "A", "B"] + ["A"] * 8)

    def test_employee_joining_mid_processing_goes_to_back(self):
        self.submit("A", 3)
        self.assertEqual(self.scheduler.get_next_request(), ("A", {"id": "A1"}))

        self.submit("B", 2)
        self.submit("C", 1)

        ids = [request["id"] for _, request in self.drain()]
        self.assertEqual(ids, ["A2", "B1", "C1", "A3", "B2"])

    def test_employee_appears_only_once_in_rotation(self):
        self.submit("A", 50)
        rotation = self.redis.lrange(f"{self.prefix}rotation", 0, -1)
        self.assertEqual(rotation, ["A"])

    def test_pods_share_one_fair_order(self):
        # A's requests arrive at pod 1, B's at pod 2, and the pods take turns
        # pulling work. Fairness has to hold across both.
        pod1, pod2 = self.make_scheduler(), self.make_scheduler()
        self.submit("A", 4, pod1)
        self.submit("B", 2, pod2)

        users = []
        for pod in [pod1, pod2] * 3:
            user, _ = pod.get_next_request()
            users.append(user)

        self.assertEqual(users, ["A", "B", "A", "B", "A", "A"])

    def test_concurrent_pods_lose_nothing_and_keep_order(self):
        num_pods, per_employee = 4, 100

        def submit_all(pod_number):
            pod = self.make_scheduler()
            for i in range(per_employee):
                pod.add_request(f"E{pod_number}", {"i": i})

        threads = [threading.Thread(target=submit_all, args=(n,)) for n in range(num_pods)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(self.scheduler.pending_count(), num_pods * per_employee)

        by_user = {}
        for user, request in self.drain():
            by_user.setdefault(user, []).append(request["i"])

        self.assertEqual(len(by_user), num_pods)
        for user, sequence in by_user.items():
            with self.subTest(employee=user):
                self.assertEqual(sequence, list(range(per_employee)))


class TestRedisWorkers(RedisTestCase):

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(scheduler_worker, "print", create=True))

    def make_pod(self, num_workers=1, inference_fn=fast_inference):
        client = self.make_client()
        pod = SchedulerWorker(
            inference_fn=inference_fn,
            scheduler=RedisFairScheduler(client, self.prefix),
            job_store=RedisJobStore(client, self.prefix),
            num_workers=num_workers,
        )
        self.addCleanup(pod.stop)
        return pod

    def submit_before_start(self, pod, requests):
        # Queue everything before the threads start so the order is predictable.
        pod.running = True
        futures = [pod.submit(user, prompt) for user, prompt in requests]
        pod.running = False
        return futures

    def test_request_gets_a_response(self):
        pod = self.make_pod()
        pod.start()

        response = pod.submit("A", "What is Redis?").result(timeout=RESULT_TIMEOUT)

        self.assertEqual(response["response"], "You asked: What is Redis?")
        self.assertEqual(pod.get_status(response["request_id"]), "COMPLETED")

    def test_request_processed_by_another_pod(self):
        # Pod 1 only accepts requests; pod 2 does the processing
        frontend = self.make_pod(num_workers=0)
        backend = self.make_pod(num_workers=1)
        frontend.start()
        backend.start()

        response = frontend.submit("A", "What is Kubernetes?").result(timeout=RESULT_TIMEOUT)

        self.assertEqual(response["response"], "You asked: What is Kubernetes?")
        # Status is shared, so either pod can report it
        self.assertEqual(backend.get_status(response["request_id"]), "COMPLETED")

    def test_fair_order_across_pods(self):
        pod1 = self.make_pod(num_workers=0)
        pod2 = self.make_pod(num_workers=0)
        processor = self.make_pod(num_workers=1)

        futures = self.submit_before_start(pod1, [("A", f"A{i}") for i in range(1, 5)])
        futures += self.submit_before_start(pod2, [("B", f"B{i}") for i in range(1, 3)])

        completed = []
        for future in futures:
            future.add_done_callback(lambda f: completed.append(f.result()["user_id"]))

        for pod in (pod1, pod2, processor):
            pod.start()
        for future in futures:
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(completed, ["A", "B", "A", "B", "A", "A"])

    def test_cancelled_request_is_skipped_by_other_pods(self):
        frontend = self.make_pod(num_workers=0)
        backend = self.make_pod(num_workers=1)

        cancelled, normal = self.submit_before_start(
            frontend, [("A", "cancel me"), ("B", "keep me")]
        )
        request_id = next(iter(frontend.jobs))
        cancelled.cancel()

        frontend.start()
        backend.start()

        self.assertEqual(normal.result(timeout=RESULT_TIMEOUT)["user_id"], "B")
        self.assertEqual(backend.get_status(request_id), "CANCELLED")

    def test_failure_reaches_the_caller_on_another_pod(self):
        frontend = self.make_pod(num_workers=0)
        backend = self.make_pod(num_workers=1, inference_fn=failing_inference)
        frontend.start()
        backend.start()

        future = frontend.submit("A", "What is Docker?")
        request_id = next(iter(frontend.jobs))

        with self.assertRaisesRegex(RuntimeError, "Simulated LLM processing failure"):
            future.result(timeout=RESULT_TIMEOUT)
        self.assertEqual(frontend.get_status(request_id), "FAILED")

    def test_stopping_a_pod_cancels_its_waiting_callers(self):
        frontend = self.make_pod(num_workers=0)
        frontend.start()
        futures = [frontend.submit("A", f"A{i}") for i in range(3)]
        request_ids = list(frontend.jobs)

        frontend.stop()

        self.assertTrue(all(f.cancelled() for f in futures))
        # Marked CANCELLED, so no other pod wastes an LLM call on them
        for request_id in request_ids:
            self.assertEqual(frontend.get_status(request_id), "CANCELLED")

    def test_job_status_expires(self):
        pod = self.make_pod()
        pod.start()
        response = pod.submit("A", "What is Redis?").result(timeout=RESULT_TIMEOUT)

        ttl = self.redis.ttl(f"{self.prefix}job:{response['request_id']}")
        self.assertGreater(ttl, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)