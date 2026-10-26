import logging
import threading
import time
import uuid
from concurrent.futures import Future, InvalidStateError

from scheduler.fair_scheduler import FairShareScheduler
from scheduler.job_store import MemoryJobStore

logger = logging.getLogger(__name__)

# How long an idle worker waits before checking the queue again. Requests
# submitted on this pod wake it immediately; requests from other pods are
# picked up on the next check.
IDLE_POLL_SECONDS = 0.5

# How long the result listener waits for a result before looping
RESULT_POLL_SECONDS = 1

# Longest a rate-limited worker sleeps before checking again
RATE_LIMIT_POLL_SECONDS = 1


class SchedulerWorker:
    def __init__(self, inference_fn=None, scheduler=None, job_store=None, num_workers=1,rate_limiter=None):
        # Assign inference function if there is no actual inference, use mock inference.
        self.inference_fn = (
            inference_fn if inference_fn is not None else self._mock_inference
        )

        # Defaults keep everything in this process. Pass the Redis versions
        # to share the queue and job statuses between pods.
        self.scheduler = scheduler if scheduler is not None else FairShareScheduler()
        self.job_store = job_store if job_store is not None else MemoryJobStore()

        # Limits how many LLM calls start per minute. None means no limit.
        self.rate_limiter = rate_limiter

        # Number of requests this instance processes at the same time.
        # 0 means it only accepts requests and other pods process them.
        self.num_workers = num_workers

        # Unique per instance, so other pods know where to send our results
        self.instance_id = uuid.uuid4().hex

        # Wake the worker when a request arrives
        self.new_request = threading.Event()

        # Allows the worker to stop cleanly
        self.running = False
        self.threads = []

        # Futures of requests submitted to this instance, by request ID.
        # Futures can't be stored in Redis, so they stay here and the
        # result is matched back to them by request ID.
        self.jobs = {}
        self.jobs_lock = threading.Lock()

    @staticmethod
    # This is for a mock response from a LLM model. NOT REAL!
    def _mock_inference(prompt: str) -> str:
        time.sleep(1)
        return f"You asked: {prompt}"

    def start(self):
        if self.running:
            return

        self.running = True

        targets = [self._process_loop] * self.num_workers + [self._result_loop]
        self.threads = [
            threading.Thread(target=target, daemon=True)
            for target in targets
        ]

        for thread in self.threads:
            thread.start()

    def submit(self, user_id, prompt):
        if not self.running:
            raise RuntimeError("Scheduler worker is not running")

        request_id = str(uuid.uuid4())
        # Future stores the result for the caller
        future = Future()

        with self.jobs_lock:
            self.jobs[request_id] = future

        future.add_done_callback(
            lambda done: self._on_future_done(request_id, done)
        )

        # Status first, so a worker never sees a request without one
        self.job_store.create(request_id, user_id)

        self.scheduler.add_request(user_id, {
            "request_id": request_id,
            "prompt": prompt,
            "reply_to": self.instance_id
        })

        # Tell the worker that work is available
        self.new_request.set()

        return future

    def _on_future_done(self, request_id, future):
        with self.jobs_lock:
            self.jobs.pop(request_id, None)

        # If the caller gave up while it was still queued, mark it so
        # no worker on any pod wastes an LLM call on it
        if future.cancelled():
            self.job_store.cancel(request_id)

    def _process_loop(self):
        while self.running:
            try:
                self._process_next()
            except Exception:
                # e.g. Redis briefly unreachable. Keep the worker alive.
                logger.exception("Scheduler worker error, retrying")
                time.sleep(1)

    def _process_next(self):
        if self.scheduler.pending_count() == 0:
            self.new_request.clear()

            # Recheck after clearing to avoid
            # missing a concurrent submission
            if self.scheduler.pending_count() == 0:
                self.new_request.wait(timeout=IDLE_POLL_SECONDS)

            return

        # Take a rate-limit slot before choosing a request, so the fair
        # choice is made when the LLM call can actually start
        if self.rate_limiter is not None:
            wait = self.rate_limiter.try_acquire()
            if wait > 0:
                time.sleep(min(wait, RATE_LIMIT_POLL_SECONDS))
                return

        result = self.scheduler.get_next_request()

        # Another worker took it first
        if result is None:
            return

        user_id, request = result
        request_id = request["request_id"]

        # Skip requests the caller already cancelled
        if not self.job_store.claim(request_id):
            return

        try:
            print(
                f"Processing request {request_id[:8]} "
                f"from {user_id}"
            )

            # Execute the configured inference function when this user gets a turn.
            answer = self.inference_fn(request["prompt"])

            response = {
                "response": answer,
                "source": "mock",
                "user_id": user_id,
                "request_id": request_id,
            }

            self.job_store.set_status(request_id, "COMPLETED")
            message = {"request_id": request_id, "response": response}

        except Exception as error:
            self.job_store.set_status(request_id, "FAILED")
            message = {"request_id": request_id, "error": error}

        # Send the result to the instance whose caller is waiting for it
        self.job_store.publish_result(request["reply_to"], message)

    def _result_loop(self):
        while self.running:
            try:
                message = self.job_store.next_result(
                    self.instance_id, timeout=RESULT_POLL_SECONDS
                )
            except Exception:
                logger.exception("Result listener error, retrying")
                time.sleep(1)
                continue

            if message is not None:
                self._resolve(message)

    def _resolve(self, message):
        with self.jobs_lock:
            future = self.jobs.get(message.get("request_id"))

        if future is None:
            return

        try:
            if "error" in message:
                future.set_exception(message["error"])
            else:
                future.set_result(message["response"])
        except InvalidStateError:
            # The caller cancelled at the same moment
            pass

    # Allow worker to change status of a request
    def update_status(self, request_id, status):
        self.job_store.set_status(request_id, status)

    def get_status(self, request_id):
        return self.job_store.get_status(request_id)

    def stop(self):
        self.running = False
        self.new_request.set()

        # Wake the result listener so it notices straight away
        try:
            self.job_store.publish_result(self.instance_id, {"request_id": None})
        except Exception:
            logger.exception("Could not wake result listener")

        for thread in self.threads:
            thread.join()
        self.threads = []

        # Cancel requests from our callers that haven't been answered, so
        # they aren't left waiting. Queued ones are marked CANCELLED, so
        # no other pod processes them either.
        with self.jobs_lock:
            futures = list(self.jobs.values())

        for future in futures:
            future.cancel()