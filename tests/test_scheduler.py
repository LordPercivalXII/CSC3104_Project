# Run 5 automated unit tests

import unittest
from threading import Thread

from scheduler.fair_scheduler import FairShareScheduler


class TestFairShareScheduler(unittest.TestCase):

    def setUp(self):
        self.scheduler = FairShareScheduler()

    def test_empty_queue(self):
        self.assertIsNone(
            self.scheduler.get_next_request()
        )
        self.assertEqual(
            self.scheduler.pending_count(), 0
        )

    def test_single_employee(self):
        self.scheduler.add_request("A", "A1")
        self.scheduler.add_request("A", "A2")

        self.assertEqual(
            self.scheduler.get_next_request(),
            ("A", "A1")
        )
        self.assertEqual(
            self.scheduler.get_next_request(),
            ("A", "A2")
        )

    def test_round_robin(self):
        for i in range(1, 4):
            self.scheduler.add_request("A", f"A{i}")

        for i in range(1, 3):
            self.scheduler.add_request("B", f"B{i}")

        self.scheduler.add_request("C", "C1")

        actual = []

        while self.scheduler.pending_count() > 0:
            actual.append(
                self.scheduler.get_next_request()[1]
            )

        expected = [
            "A1", "B1", "C1",
            "A2", "B2", "A3"
        ]

        self.assertEqual(actual, expected)

    def test_employee_returns(self):
        self.scheduler.add_request("A", "A1")
        self.assertEqual(
            self.scheduler.get_next_request(),
            ("A", "A1")
        )

        self.scheduler.add_request("A", "A2")
        self.assertEqual(
            self.scheduler.get_next_request(),
            ("A", "A2")
        )

    def test_concurrent_submissions(self):
        def submit(user):
            for i in range(100):
                self.scheduler.add_request(
                    user, f"{user}{i}"
                )

        threads = [
            Thread(target=submit, args=(user,))
            for user in ["A", "B", "C"]
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        self.assertEqual(
            self.scheduler.pending_count(), 300
        )

        results = []

        while self.scheduler.pending_count() > 0:
            results.append(
                self.scheduler.get_next_request()
            )

        self.assertEqual(len(results), 300)
        self.assertEqual(len(set(results)), 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
