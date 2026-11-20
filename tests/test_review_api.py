import asyncio
import threading
from concurrent.futures import Future
from unittest import mock

import pytest
from fastapi import BackgroundTasks, HTTPException

from test_chat_api import main
from scheduler.errors import QueueFullError
from scheduler.redis_backend import RemoteInferenceError


@pytest.mark.parametrize("rate_limited,status", [(False, 503), (True, 429)])
def test_provider_errors_are_generic_and_preserve_rate_limit(rate_limited, status):
    future = Future()
    future.set_exception(RemoteInferenceError("secret provider diagnostic", rate_limited))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.wait_for_llm(future))
    assert error.value.status_code == status
    assert "secret" not in error.value.detail
    if rate_limited:
        assert error.value.headers["Retry-After"]


def test_full_queue_returns_retryable_429():
    with mock.patch.object(main.scheduler_worker, "submit", side_effect=QueueFullError("internal")):
        with pytest.raises(HTTPException) as error:
            asyncio.run(main._submit_and_wait("A", "question"))
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"]


def test_deadline_cancels_future_without_blocking_event_loop():
    future = Future()
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    future.add_done_callback(lambda done: cancelled.set())
    def slow_submit(*args, **kwargs):
        entered.set()
        release.wait(2)
        return future
    async def run():
        ticks = []
        async def tick():
            await asyncio.sleep(0.01)
            ticks.append(True)
        timer = asyncio.create_task(tick())
        waiting = asyncio.create_task(main._submit_and_wait("A", "question"))
        assert await asyncio.to_thread(entered.wait, 1)
        async def operation():
            return await waiting
        with pytest.raises(HTTPException) as error:
            await main._within_deadline(operation())
        await timer
        assert ticks
        assert error.value.status_code == 504
        release.set()  # submission completes after route cancellation
        assert await asyncio.to_thread(cancelled.wait, 1)
        assert future.cancelled()
    with mock.patch.object(main, "_request_timeout_seconds", 0.03), mock.patch.object(main.scheduler_worker, "submit", slow_submit):
        asyncio.run(run())


def test_client_disconnect_cancels_pending_inference():
    future = Future()
    submitted = []
    def submit(*args, **kwargs):
        submitted.append(True)
        return future
    class Disconnected:
        async def is_disconnected(self):
            while not submitted:
                await asyncio.sleep(0.001)
            return True
    async def run():
        with pytest.raises(HTTPException) as error:
            await main._within_deadline(main._submit_and_wait("A", "question"), Disconnected())
        assert error.value.status_code == 499
        assert future.cancelled()
    with mock.patch.object(main.scheduler_worker, "submit", side_effect=submit):
        asyncio.run(run())


def test_readiness_checks_collection_and_redis_but_liveness_stays_up(monkeypatch):
    monkeypatch.setenv("SCHEDULER_BACKEND", "redis")
    with mock.patch.object(main.semantic_cache.client, "collection_exists", return_value=True), mock.patch("redis.Redis.from_url") as factory:
        factory.return_value.ping.side_effect = RuntimeError("private connection data")
        with pytest.raises(HTTPException) as error:
            main.ready()
        assert error.value.status_code == 503
        assert "private" not in error.value.detail
        assert main.health() == {"status": "healthy"}
        factory.return_value.close.assert_called_once()


def test_finished_response_does_not_hang_cancelling_disconnect_probe():
    async def run():
        entered = asyncio.Event()
        class Probe:
            async def is_disconnected(self):
                # Exercise cancellation arriving while inside a receive scope.
                entered.set()
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    pass
                return False
        async def operation():
            await entered.wait()
            return "answer"
        assert await asyncio.wait_for(main._within_deadline(operation(), Probe()), 0.2) == "answer"
        await asyncio.sleep(0.03)
    asyncio.run(run())
