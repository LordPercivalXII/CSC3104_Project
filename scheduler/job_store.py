"""
In-memory job store, used by default when everything runs in one process.

The job store keeps track of each request's status and passes finished
results back to whoever is waiting. RedisJobStore (redis_backend.py) has
the same methods but shares this data between pods.
"""
import queue
import threading


class MemoryJobStore:

    def __init__(self):
        # request_id -> {"user_id", "status"}
        self.jobs = {}

        # reply_to -> queue of finished results waiting to be picked up
        self.results = {}

        # Worker threads and request threads both use these dicts
        self.lock = threading.Lock()

    def create(self, request_id, user_id):
        with self.lock:
            self.jobs[request_id] = {
                "user_id": user_id,
                "status": "PENDING"
            }

    # claim() and cancel() can race: a worker may claim a request at the same
    # moment its caller cancels it. Both only succeed from PENDING, so
    # exactly one of them wins.

    def claim(self, request_id):
        """PENDING -> PROCESSING. False if the caller already cancelled."""
        return self._change(request_id, "PENDING", "PROCESSING")

    def cancel(self, request_id):
        """PENDING -> CANCELLED. False if it is already being processed."""
        return self._change(request_id, "PENDING", "CANCELLED")

    def _change(self, request_id, expected, new):
        # Check and update under one lock so no other thread can change
        # the status in between
        with self.lock:
            job = self.jobs.get(request_id)
            if job is None or job["status"] != expected:
                return False

            job["status"] = new
            return True

    def set_status(self, request_id, status):
        with self.lock:
            if request_id not in self.jobs:
                raise KeyError(f"Unknown request ID: {request_id}")

            self.jobs[request_id]["status"] = status

    def get_status(self, request_id):
        """Return the request's status, or None if the ID is unknown."""
        with self.lock:
            job = self.jobs.get(request_id)

            if job is None:
                return None

            return job["status"]

    def publish_result(self, reply_to, message):
        """Send a finished result to the worker instance that is waiting for it."""
        self._results_for(reply_to).put(message)

    def next_result(self, reply_to, timeout):
        """Wait up to `timeout` seconds for a result addressed to `reply_to`."""
        try:
            return self._results_for(reply_to).get(timeout=timeout)
        except queue.Empty:
            return None

    def _results_for(self, reply_to):
        # Create the result queue the first time an instance is used
        with self.lock:
            return self.results.setdefault(reply_to, queue.Queue())