import threading
import time
from urllib import request
import uuid
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

        # Track request information
        self.jobs = {}
        self.jobs_lock = threading.Lock()

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

        request_id = str(uuid.uuid4())
        # Future stores the result for the caller
        future = Future()

        request = {
            "request_id": request_id,
            "prompt": prompt,
            "future": future
        }

        with self.jobs_lock:
            self.jobs[request_id] = {
            "user_id": user_id,
            "status": "PENDING"
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
            request_id = request["request_id"]

            try:
                self.update_status(request_id, "PROCESSING")

                print(
                    f"Processing request {request_id[:8]} "
                    f"from {user_id}"
                )

                # Simulate LLM inference
                time.sleep(1)

                response = {
                    "response": f"You asked: {request['prompt']}",
                    "source": "mock",
                    "user_id": user_id,
                    "request_id": request_id
                }

                self.update_status(request_id, "COMPLETED")

                request["future"].set_result(response)

            except Exception as error:
                self.update_status(request_id, "FAILED")
                request["future"].set_exception(error)

    # Allow worker to change status of a request
    def update_status(self, request_id, status):
        with self.jobs_lock:
            if request_id not in self.jobs:
                raise KeyError(f"Unknown request ID: {request_id}")

        self.jobs[request_id]["status"] = status

    def get_status(self, request_id):
        with self.jobs_lock:
            job = self.jobs.get(request_id)

            if job is None:
                return None

        return job["status"]

    def stop(self):
        self.running = False
        self.new_request.set()

        if self.thread:
            self.thread.join()
