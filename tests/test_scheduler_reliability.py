"""Failure injection for memory and Redis coordination; no server or model needed."""
import threading
import time
import uuid

import fakeredis
import pytest

from scheduler.errors import QueueFullError, SchedulerTimeoutError, SchedulerUnavailableError
from scheduler.fair_scheduler import FairShareScheduler
from scheduler.job_store import MemoryJobStore
from scheduler.redis_backend import RedisFairScheduler, RedisJobStore
from scheduler.reliability import MemoryCoordinator, RedisCoordinator
from scheduler.scheduler_worker import SchedulerWorker


@pytest.fixture(params=["memory", "redis"])
def coord(request):
    config = dict(timeout=2, lease=0.03, attempts=2, max_pending=20, max_per_user=10, retention=3600)
    if request.param == "memory":
        return MemoryCoordinator(FairShareScheduler(), MemoryJobStore(), config)
    client = fakeredis.FakeRedis(decode_responses=True)
    return RedisCoordinator(RedisFairScheduler(client, "test:"), RedisJobStore(client, "test:"), config)


def request(user="A", key=None):
    value = dict(request_id=uuid.uuid4().hex, user_id=user, prompt="question", reply_to="frontend")
    if key:
        value["dedup_key"] = key
    return value


def answer(r):
    return dict(request_id=r["request_id"], response=dict(request_id=r["request_id"], user_id=r["user_id"], response="ok"))


def test_abandoned_lease_recovers_and_fences_stale_completion(coord):
    r = request()
    coord.submit(r)
    _, abandoned = coord.take()
    time.sleep(0.04)
    coord.maintain()
    _, recovered = coord.take()
    assert recovered["lease_token"] != abandoned["lease_token"]
    assert not coord.complete(abandoned, answer(abandoned))
    assert not coord.renew(abandoned)
    assert coord.complete(recovered, answer(recovered))
    assert coord.result(r["request_id"])["response"]["response"] == "ok"


def test_recovery_attempts_are_bounded(coord):
    r = request()
    coord.submit(r)
    for _ in range(2):
        assert coord.take()
        time.sleep(0.04)
        coord.maintain()
    assert isinstance(coord.result(r["request_id"])["error"], SchedulerUnavailableError)
    assert coord.take() is None


def test_deadline_finishes_queued_requests_and_followers(coord):
    coord.config["timeout"] = 0.01
    leader, follower = request(key="same"), request("B", "same")
    coord.submit(leader)
    coord.submit(follower)
    time.sleep(0.02)
    coord.maintain()
    for r in (leader, follower):
        assert isinstance(coord.result(r["request_id"])["error"], SchedulerTimeoutError)
    assert coord.take() is None


def test_late_completion_cannot_publish_success(coord):
    coord.config["timeout"] = 0.01
    r = request()
    coord.submit(r)
    _, processing = coord.take()
    time.sleep(0.02)
    coord.complete(processing, answer(processing))
    assert isinstance(coord.result(r["request_id"])["error"], SchedulerTimeoutError)


def test_pending_state_loss_returns_error_instead_of_silent_drop(coord):
    r = request()
    coord.submit(r)
    if isinstance(coord, RedisCoordinator):
        coord.redis.delete(coord.store.job_prefix + r["request_id"])
    else:
        coord.store.jobs.pop(r["request_id"])
    assert coord.take() is None
    assert isinstance(coord.result(r["request_id"])["error"], SchedulerUnavailableError)


def test_cancelled_follower_stays_cancelled_after_leader_completes(coord):
    leader, follower = request(key="same"), request("B", "same")
    coord.submit(leader)
    _, processing = coord.take()
    coord.submit(follower)
    coord.cancel(follower["request_id"])
    coord.complete(processing, answer(processing))
    assert coord.store.get_status(follower["request_id"]) == "CANCELLED"
    assert coord.result(follower["request_id"]) is None


def test_follower_fair_turn_can_run_shared_call_ahead_of_leader_backlog(coord):
    backlog, leader, follower = request(), request(key="same"), request("B", "same")
    coord.submit(backlog)
    coord.submit(leader)
    coord.submit(follower)
    assert coord.take()[1]["request_id"] == backlog["request_id"]
    user, shared = coord.take()
    assert user == "B"
    assert shared["request_id"] == leader["request_id"]
    coord.complete(shared, answer(shared))
    assert coord.result(follower["request_id"])["response"]["coalesced"]
    assert coord.take() is None


def test_admission_limit_includes_coalesced_waiters(coord):
    coord.config["max_per_user"] = 1
    coord.submit(request(key="same"))
    with pytest.raises(QueueFullError):
        coord.submit(request(key="same"))
    coord.config["max_pending"] = 1
    with pytest.raises(QueueFullError):
        coord.submit(request("B"))


def test_cancelled_leader_continues_for_remaining_follower(coord):
    leader, follower = request(key="same"), request("B", "same")
    coord.submit(leader)
    coord.submit(follower)
    coord.cancel(leader["request_id"])
    _, processing = coord.take()
    coord.complete(processing, answer(processing))
    assert coord.result(follower["request_id"])["response"]["response"] == "ok"


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_lost_notification_is_recovered_without_repeating_inference(backend, monkeypatch):
    calls = []
    options = {}
    if backend == "redis":
        client = fakeredis.FakeRedis(decode_responses=True)
        options = dict(scheduler=RedisFairScheduler(client, "test:"), job_store=RedisJobStore(client, "test:"))
    worker = SchedulerWorker(inference_fn=lambda p: calls.append(p) or "answer", **options)
    monkeypatch.setattr(worker.job_store, "next_result", lambda *a, **k: time.sleep(0.01))
    worker.start()
    try:
        assert worker.submit("A", "question").result(2)["response"] == "answer"
        assert calls == ["question"]
    finally:
        worker.stop()


def test_submit_failure_cleans_local_future_and_coalescing_state(monkeypatch):
    worker = SchedulerWorker(num_workers=0)
    worker.running = True
    monkeypatch.setattr(worker.scheduler, "add_request", lambda *a: (_ for _ in ()).throw(RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        worker.submit("A", "question", dedup=True)
    assert not worker.jobs
    assert not worker.coordinator.inflight
    worker.stop()


def test_stop_has_bound_even_when_inference_does_not_return():
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt):
        entered.set()
        release.wait(2)
        return "answer"
    worker = SchedulerWorker(inference_fn=blocked, shutdown_timeout=0.05)
    worker.start()
    future = worker.submit("A", "question")
    assert entered.wait(1)
    started = time.monotonic()
    worker.stop()
    release.set()
    assert time.monotonic() - started < 0.3
    assert future.cancelled()
