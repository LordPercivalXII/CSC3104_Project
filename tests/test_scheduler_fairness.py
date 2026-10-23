"""
Fairness guarantees and worker reliability tests.

Complements test_scheduler.py (basic queue behaviour) by checking the
fairness properties in more depth and testing SchedulerWorker end to end.

Run from the tests folder:
    python test_scheduler_fairness.py
    python test_scheduler_fairness.py --watch   # print each step
"""
import os
import sys
import threading
import time
import types
import unittest
from collections import deque
from concurrent.futures import CancelledError
from concurrent.futures import TimeoutError as FutureTimeout
from statistics import mean
from unittest import mock

# Makes "scheduler" importable when running from inside tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduler import scheduler_worker                  # noqa: E402
from scheduler.fair_scheduler import FairShareScheduler  # noqa: E402
from scheduler.scheduler_worker import SchedulerWorker   # noqa: E402

WATCH = "--watch" in sys.argv
if WATCH:
    sys.argv.remove("--watch")  # so unittest doesn't treat it as its own option

# Replaces the worker's 1s sleep. Slower in watch mode so the steps are readable.
FAKE_LLM_SECONDS = 0.3 if WATCH else 0.05
RESULT_TIMEOUT = 5


def show_state(scheduler):
    rotation = list(scheduler.active_users)
    queues = ", ".join(f"{user}:{len(q)}" for user, q in scheduler.queues.items())
    return f"rotation={rotation}  queued={{{queues}}}"


def drain(scheduler, show=True):
    """Return every request in the order the scheduler hands them out."""
    if WATCH and show:
        print(f"\n    start   {show_state(scheduler)}")

    order = []
    while (item := scheduler.get_next_request()) is not None:
        order.append(item)
        if WATCH and show:
            user, request = item
            print(f"    served {user} -> {request:<4} {show_state(scheduler)}")
    return order


def average_wait_per_employee(order):
    """Each request takes 1 time unit and all arrive at time 0."""
    waits = {}
    for position, (user, _) in enumerate(order):
        waits.setdefault(user, []).append(position)
    return {user: mean(times) for user, times in waits.items()}


class TestFairnessGuarantees(unittest.TestCase):

    def setUp(self):
        self.scheduler = FairShareScheduler()

    def submit(self, user, count):
        for i in range(1, count + 1):
            self.scheduler.add_request(user, f"{user}{i}")

    def test_heavy_employee_cannot_starve_light_employee(self):
        # B arrives after A's 10 requests but is still served 2nd and 4th
        self.submit("A", 10)
        self.submit("B", 2)

        users = [user for user, _ in drain(self.scheduler)]
        self.assertEqual(users, ["A", "B", "A", "B"] + ["A"] * 8)

    def test_employee_joining_mid_processing_goes_to_back(self):
        self.submit("A", 3)
        self.assertEqual(self.scheduler.get_next_request(), ("A", "A1"))

        # B and C join while A1 is being processed, in that order
        self.submit("B", 2)
        self.submit("C", 1)

        requests = [req for _, req in drain(self.scheduler)]
        self.assertEqual(requests, ["A2", "B1", "C1", "A3", "B2"])

    def test_employee_appears_only_once_in_rotation(self):
        # duplicates would give the employee extra turns
        self.submit("A", 50)
        self.assertEqual(list(self.scheduler.active_users).count("A"), 1)

    def test_concurrent_submissions_keep_each_employees_order(self):
        num_employees, per_employee = 8, 500

        def submit_all(user):
            for i in range(1, per_employee + 1):
                self.scheduler.add_request(user, i)

        threads = [
            threading.Thread(target=submit_all, args=(f"E{n}",))
            for n in range(num_employees)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        by_user = {}
        for user, i in drain(self.scheduler, show=False):  # 4000 lines is too much
            by_user.setdefault(user, []).append(i)

        for user, sequence in by_user.items():
            with self.subTest(employee=user):
                self.assertEqual(sequence, list(range(1, per_employee + 1)))

    def test_light_employees_wait_less_than_with_fifo(self):
        # Same workload as test_scheduler_performance.py
        workload = (
            [("A", f"A{i}") for i in range(1, 11)]
            + [("B", f"B{i}") for i in range(1, 3)]
            + [("C", f"C{i}") for i in range(1, 3)]
        )
        for user, request in workload:
            self.scheduler.add_request(user, request)

        fifo = average_wait_per_employee(list(deque(workload)))
        fair = average_wait_per_employee(drain(self.scheduler, show=False))

        if WATCH:
            print(f"\n    FIFO avg wait: {fifo}\n    fair avg wait: {fair}")

        self.assertLess(fair["B"], fifo["B"])
        self.assertLess(fair["C"], fifo["C"])
        # same total work, so the heavy employee pays for it
        self.assertGreater(fair["A"], fifo["A"])


class WorkerTestCase(unittest.TestCase):

    def setUp(self):
        fast_time = types.SimpleNamespace(sleep=lambda _: time.sleep(FAKE_LLM_SECONDS))
        self.enterContext(mock.patch.object(scheduler_worker, "time", fast_time))
        if WATCH:
            print()
        else:
            self.enterContext(mock.patch.object(scheduler_worker, "print", create=True))

        self.worker = SchedulerWorker()
        self.addCleanup(self.worker.stop)

    def submit_before_start(self, requests):
        # Queue everything before the thread starts so the order is predictable.
        # submit() needs running=True, so we set it briefly.
        self.worker.running = True
        futures = [self.worker.submit(user, prompt) for user, prompt in requests]
        self.worker.running = False
        return futures


class TestWorkerReliability(WorkerTestCase):

    def test_submit_before_start_raises_error(self):
        with self.assertRaises(RuntimeError):
            self.worker.submit("A", "What is Docker?")

    def test_response_and_status_are_returned(self):
        self.worker.start()
        future = self.worker.submit("A", "What is Kubernetes?")

        response = future.result(timeout=RESULT_TIMEOUT)
        self.assertEqual(response["response"], "You asked: What is Kubernetes?")
        self.assertEqual(response["user_id"], "A")
        self.assertEqual(self.worker.get_status(response["request_id"]), "COMPLETED")

    def test_unknown_request_id_has_no_status(self):
        self.assertIsNone(self.worker.get_status("does-not-exist"))

    def test_worker_processes_employees_fairly(self):
        futures = self.submit_before_start([
            ("A", "What is Docker?"),
            ("A", "What is Kubernetes?"),
            ("A", "Explain cloud computing"),
            ("B", "What is Redis?"),
            ("B", "Explain caching"),
            ("C", "What is FastAPI?"),
        ])

        completed = []
        for future in futures:
            future.add_done_callback(lambda f: completed.append(f.result()["user_id"]))

        self.worker.start()
        for future in futures:
            future.result(timeout=RESULT_TIMEOUT)

        self.assertEqual(completed, ["A", "B", "C", "A", "B", "A"])

    def test_cancelled_request_is_skipped(self):
        cancelled, normal = self.submit_before_start([
            ("A", "What is Docker?"),
            ("B", "What is Redis?"),
        ])
        cancelled.cancel()
        self.worker.start()

        response = normal.result(timeout=RESULT_TIMEOUT)
        self.assertEqual(response["user_id"], "B")
        self.assertTrue(cancelled.cancelled())

    def test_stop_resolves_pending_requests(self):
        self.worker.start()
        futures = [self.worker.submit("A", f"A{i}") for i in range(1, 11)]
        self.worker.stop()

        for future in futures:
            try:
                future.result(timeout=0.5)
            except CancelledError:
                pass
            except FutureTimeout:
                self.fail("request left hanging after stop()")


if __name__ == "__main__":
    unittest.main(verbosity=2)