
import time
import unittest

from scheduler.scheduler_worker import SchedulerWorker


class TestJobStatus(unittest.TestCase):

    def test_status_transitions(self):
        worker = SchedulerWorker()

        # Queue the request before starting the worker
        # so we can reliably inspect PENDING.
        worker.running = True

        future = worker.submit("A", "What is Docker?")

        # Find the ID of our single submitted request.
        request_id = next(iter(worker.jobs))

        self.assertEqual(
            worker.get_status(request_id),
            "PENDING"
        )

        # Start the processing thread.
        worker.running = False
        worker.start()

        try:
            # Wait briefly for the worker to start processing.
            deadline = time.monotonic() + 2

            while (
                worker.get_status(request_id) == "PENDING"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            self.assertEqual(
                worker.get_status(request_id),
                "PROCESSING"
            )

            # Wait until processing finishes.
            response = future.result(timeout=5)

            self.assertEqual(
                worker.get_status(request_id),
                "COMPLETED"
            )

            self.assertEqual(
                response["request_id"],
                request_id
            )

            print("PENDING -> PROCESSING -> COMPLETED")
            print("Request ID:", request_id)
            print("All status checks passed!")

        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
