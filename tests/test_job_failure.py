
import unittest

from scheduler.scheduler_worker import SchedulerWorker


class TestJobFailure(unittest.TestCase):

    def test_failed_request(self):
        worker = SchedulerWorker()
        worker.start()

        try:
            # Submit a request designed to trigger an error
            future = worker.submit("A", "TEST_FAILURE")

            # Get the ID of our single request
            request_id = next(iter(worker.jobs))

            # Confirm that processing raises an error
            with self.assertRaisesRegex(
                RuntimeError,
                "Simulated LLM processing failure"
            ):
                future.result(timeout=5)

            # Confirm the job is marked FAILED
            self.assertEqual(
                worker.get_status(request_id),
                "FAILED"
            )

            print("Request ID:", request_id)
            print("Final status:", worker.get_status(request_id))
            print("Failure handling test passed!")

        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
