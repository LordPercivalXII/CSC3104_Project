import unittest

from scheduler.scheduler_worker import SchedulerWorker


class TestJobFailure(unittest.TestCase):

    def test_failed_request(self):

        # Simulate an LLM that fails for a specific request.
        def simulated_inference(prompt):
            if prompt == "TEST_FAILURE":
                raise RuntimeError(
                    "Simulated LLM processing failure"
                )

            return f"You asked: {prompt}"

        # Inject our simulated inference function.
        worker = SchedulerWorker(
            inference_fn=simulated_inference
        )

        worker.start()

        try:
            # Submit a request that will fail.
            future = worker.submit("A", "TEST_FAILURE")

            # Job statuses remain in the job store
            # even after the Future is completed.
            request_id = next(iter(worker.job_store.jobs))

            # Verify the exception reaches the caller.
            with self.assertRaisesRegex(
                RuntimeError,
                "Simulated LLM processing failure"
            ):
                future.result(timeout=5)

            # Verify the scheduler marked the job FAILED.
            self.assertEqual(
                worker.get_status(request_id),
                "FAILED"
            )

            print("Failed request ID:", request_id)
            print("Status:", worker.get_status(request_id))

            # Verify the worker can process another request
            # after the previous inference failed.
            next_future = worker.submit(
                "B", "Healthy request"
            )

            result = next_future.result(timeout=5)

            self.assertEqual(
                result["response"],
                "You asked: Healthy request"
            )

            self.assertEqual(
                worker.get_status(result["request_id"]),
                "COMPLETED"
            )

            print("Next request:", result["response"])
            print("Worker recovery test passed!")

        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
