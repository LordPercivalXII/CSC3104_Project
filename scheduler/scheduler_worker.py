"""Fair queue execution with deadlines, recoverable leases and retained results."""
import hashlib
import logging
import os
import threading
import time
from time import monotonic
import uuid
from concurrent.futures import Future, InvalidStateError

from scheduler.errors import SchedulerUnavailableError
from scheduler.fair_scheduler import FairShareScheduler
from scheduler.job_store import MemoryJobStore
from scheduler.reliability import MemoryCoordinator, RedisCoordinator

logger = logging.getLogger(__name__)
IDLE_POLL_SECONDS = 0.2
RESULT_POLL_SECONDS = 0.2
RATE_LIMIT_POLL_SECONDS = 0.2


class SchedulerWorker:
    def __init__(self, inference_fn=None, scheduler=None, job_store=None, num_workers=1,
                 rate_limiter=None, coalesce=True, request_timeout=None, lease_seconds=None,
                 max_attempts=None, shutdown_timeout=None, max_pending=None, max_per_user=None):
        self.inference_fn = inference_fn if inference_fn is not None else self._mock_inference
        self.scheduler = scheduler if scheduler is not None else FairShareScheduler()
        self.job_store = job_store if job_store is not None else MemoryJobStore()
        self.rate_limiter, self.coalesce, self.num_workers = rate_limiter, coalesce, num_workers
        def setting(value, name, default, cast=float):
            result = cast(os.getenv(name, default) if value is None else value)
            if result <= 0:
                raise ValueError(name + " must be positive")
            return result
        config = {
            "timeout": setting(request_timeout, "SCHEDULER_REQUEST_TIMEOUT_SECONDS", 120),
            "lease": setting(lease_seconds, "SCHEDULER_LEASE_SECONDS", 30),
            "attempts": setting(max_attempts, "SCHEDULER_MAX_ATTEMPTS", 2, int),
            "max_pending": setting(max_pending, "SCHEDULER_MAX_PENDING", 1000, int),
            "max_per_user": setting(max_per_user, "SCHEDULER_MAX_PENDING_PER_USER", 100, int),
            "retention": 3600,
        }
        if num_workers < 0:
            raise ValueError("SCHEDULER_WORKERS cannot be negative")
        self.shutdown_timeout = setting(shutdown_timeout, "SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", 5)
        coordinator = RedisCoordinator if hasattr(self.scheduler, "redis") else MemoryCoordinator
        self.coordinator = coordinator(self.scheduler, self.job_store, config)
        self.instance_id = uuid.uuid4().hex
        self.new_request = threading.Event()
        self.running, self.threads = False, []
        self.jobs, self.leading = {}, {}
        self.jobs_lock = threading.Lock()

    @staticmethod
    def _mock_inference(prompt):
        time.sleep(1)
        return f"You asked: {prompt}"

    @staticmethod
    def _dedup_key(prompt):
        return hashlib.sha256(" ".join(prompt.casefold().split()).encode()).hexdigest()

    def start(self):
        if self.running:
            return
        self.running = True
        targets = [self._process_loop] * self.num_workers + [self._result_loop]
        self.threads = [threading.Thread(target=target, daemon=True) for target in targets]
        for thread in self.threads:
            thread.start()

    def submit(self, user_id, prompt, dedup=False):
        if not self.running:
            raise SchedulerUnavailableError("Scheduler worker is not running")
        rid, future = str(uuid.uuid4()), Future()
        request = dict(request_id=rid, prompt=prompt, user_id=user_id, reply_to=self.instance_id)
        if dedup and self.coalesce:
            request["dedup_key"] = self._dedup_key(prompt)
        with self.jobs_lock:
            self.jobs[rid] = future
        future.add_done_callback(lambda done: self._on_future_done(rid, done))
        try:
            leader = self.coordinator.submit(request)
            if request.get("dedup_key") and not leader:
                with self.jobs_lock:
                    self.leading[rid] = request["dedup_key"]
        except Exception:
            future.cancel()
            raise
        self.new_request.set()
        return future

    def _on_future_done(self, rid, future):
        with self.jobs_lock:
            self.jobs.pop(rid, None)
            self.leading.pop(rid, None)
        if future.cancelled():
            try:
                self.coordinator.cancel(rid)
            except Exception:
                logger.warning("Could not cancel shared job; its deadline still applies")

    def _process_loop(self):
        while self.running:
            try:
                self._process_next()
            except Exception:
                logger.warning("Scheduler backend unavailable; retrying")
                time.sleep(0.1)

    def _process_next(self):
        if not self.scheduler.pending_count():
            self.new_request.clear()
            if not self.scheduler.pending_count():
                self.new_request.wait(IDLE_POLL_SECONDS)
            return
        if self.rate_limiter is not None:
            wait = self.rate_limiter.try_acquire()
            if wait > 0:
                time.sleep(min(wait, RATE_LIMIT_POLL_SECONDS))
                return
        item = self.coordinator.take()
        if item is None:
            self._release_slot()
            return
        _, request = item
        rid = request["request_id"]
        stopped = threading.Event()
        def heartbeat():
            while not stopped.wait(min(1, self.coordinator.config["lease"] / 3)):
                if not self.running:
                    return
                try:
                    if not self.coordinator.renew(request):
                        return
                except Exception:
                    logger.warning("Could not renew processing lease")
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            try:
                result = self.inference_fn(request["prompt"])
                if hasattr(result, "text") and hasattr(result, "usage"):
                    answer = result.text
                    extra = dict(cacheable=result.cacheable, usage=result.usage, finish_reason=result.finish_reason)
                else:
                    answer = result
                    extra = dict(cacheable=True, usage=None, finish_reason=None)
                response = dict(response=answer, source="mock", user_id=request["user_id"], request_id=rid, **extra)
                message = dict(request_id=rid, response=response)
            except Exception as error:
                message = dict(request_id=rid, error=error)
            # Retry publication without repeating inference. The lease fences a stale worker.
            until = monotonic() + self.coordinator.config["timeout"]
            while True:
                try:
                    self.coordinator.complete(request, message)
                    break
                except Exception:
                    if not self.running or monotonic() >= until:
                        break
                    time.sleep(0.1)
        finally:
            stopped.set()

    def _release_slot(self):
        if self.rate_limiter is not None:
            self.rate_limiter.release()

    def _result_loop(self):
        while self.running:
            try:
                self.coordinator.maintain()
                message = self.job_store.next_result(self.instance_id, timeout=RESULT_POLL_SECONDS)
                if message is not None:
                    self._resolve(message)
                with self.jobs_lock:
                    ids = list(self.jobs)
                for result in self.coordinator.results(ids):
                    if result:
                        self._resolve(result)
            except Exception:
                logger.warning("Result backend unavailable; retrying")
                time.sleep(0.1)

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
            pass

    def update_status(self, request_id, status):
        self.job_store.set_status(request_id, status)

    def get_status(self, request_id):
        return self.job_store.get_status(request_id)

    def stop(self):
        self.running = False
        self.new_request.set()
        with self.jobs_lock:
            futures = list(self.jobs.values())
        def cancel_callers():
            for future in futures:
                future.cancel()
        cancellation = threading.Thread(target=cancel_callers, daemon=True)
        cancellation.start()
        until = monotonic() + self.shutdown_timeout
        for thread in [cancellation, *self.threads]:
            thread.join(max(0, until - monotonic()))
        self.threads = []
