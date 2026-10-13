import threading
import time
from concurrent.futures import Future

from scheduler.fair_scheduler import FairShareScheduler


class SchedulerWorker:
    def __init__(self):
        self.scheduler = FairShareScheduler()

        # Wake the worker when a request arrives
        self.new_request = threading.Event()

        # Allows the worker to stop cleanly
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return

        self.running = True

        self.thread = threading.Thread(
            target=self._process_loop,
            daemon=True
        )

        self.thread.start()

    def submit(self, user_id, prompt):
        if not self.running:
            raise RuntimeError("Scheduler worker is not running")

        # Future stores the result for the caller
        future = Future()

        request = {
            "prompt": prompt,
            "future": future
        }

        self.scheduler.add_request(user_id, request)

        # Tell the worker that work is available
        self.new_request.set()

        return future

    def _process_loop(self):
        while self.running:
            result = self.scheduler.get_next_request()

            if result is None:
                self.new_request.clear()

                # Recheck after clearing to avoid
                # missing a concurrent submission
                if self.scheduler.pending_count() == 0:
                    self.new_request.wait(timeout=0.5)

                continue

            user_id, request = result

            try:
                print(f"Processing request from {user_id}")

                # Simulate LLM inference
                time.sleep(1)

                response = {
                    "response": f"You asked: {request['prompt']}",
                    "source": "mock",
                    "user_id": user_id
                }

                request["future"].set_result(response)

            except Exception as error:
                request["future"].set_exception(error)

    def stop(self):
        self.running = False
        self.new_request.set()

        if self.thread:
            self.thread.join()
