import time

import fakeredis
import pytest

from scheduler.factory import create_scheduler_worker
from scheduler.rate_limiter import MemoryPacedRateLimiter
from scheduler.redis_backend import RedisPacedRateLimiter, RedisRateLimiter


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_paced_mode_does_not_allow_initial_burst(backend):
    if backend == "memory":
        limiter = MemoryPacedRateLimiter(3, 0.3)
        other = limiter
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
        limiter, other = [RedisPacedRateLimiter(client, 3, 0.3) for _ in range(2)]
    assert limiter.try_acquire() == 0
    assert other.try_acquire() > 0
    time.sleep(0.11)
    assert other.try_acquire() == 0


def test_shared_release_keeps_another_pods_reservation():
    client = fakeredis.FakeRedis(decode_responses=True)
    a, b = [RedisRateLimiter(client, 2) for _ in range(2)]
    assert a.try_acquire() == b.try_acquire() == 0
    newest = b.reservation.value
    a.release()
    assert client.zrange(a.key, 0, -1) == [newest]


def test_factory_preserves_sliding_default_and_supports_paced(monkeypatch):
    monkeypatch.setenv("SCHEDULER_BACKEND", "memory")
    monkeypatch.setenv("LLM_RATE_LIMIT_PER_MINUTE", "9")
    monkeypatch.setenv("LLM_RATE_LIMIT_MODE", "paced")
    assert isinstance(create_scheduler_worker().rate_limiter, MemoryPacedRateLimiter)
