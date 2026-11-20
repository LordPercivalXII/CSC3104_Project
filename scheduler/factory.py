"""
Builds the SchedulerWorker from environment variables.

    SCHEDULER_BACKEND          "memory" (default) or "redis"
    REDIS_URL                  Redis to use in redis mode (same variable as the cache)
    SCHEDULER_KEY_PREFIX       prefix for the scheduler's Redis keys (default "sched:")
    SCHEDULER_WORKERS          requests this pod processes at once (default 1)
    LLM_RATE_LIMIT_PER_MINUTE  max LLM calls started per minute, 0 = no limit (default).
                               Shared by all pods in redis mode, per pod in memory mode.
    SCHEDULER_COALESCE         "true" (default): identical questions asked at the same
                               time share one LLM call. "false" turns it off.
"""
import os

from scheduler.rate_limiter import MemoryRateLimiter, MemoryPacedRateLimiter
from scheduler.scheduler_worker import SchedulerWorker


def create_scheduler_worker(inference_fn=None):
    backend = os.getenv("SCHEDULER_BACKEND", "memory").lower()
    num_workers = int(os.getenv("SCHEDULER_WORKERS", "1"))
    rate_limit = int(os.getenv("LLM_RATE_LIMIT_PER_MINUTE", "0"))
    rate_mode = os.getenv("LLM_RATE_LIMIT_MODE", "sliding").lower()
    if rate_mode not in ("sliding", "paced"):
        raise ValueError("LLM_RATE_LIMIT_MODE must be sliding or paced")
    if rate_limit < 0:
        raise ValueError("LLM_RATE_LIMIT_PER_MINUTE cannot be negative")
    coalesce = os.getenv("SCHEDULER_COALESCE", "true").lower() not in ("0", "false", "no", "off")

    if backend == "memory":
        return SchedulerWorker(
            inference_fn=inference_fn,
            num_workers=num_workers,
            rate_limiter=(MemoryPacedRateLimiter if rate_mode == "paced" else MemoryRateLimiter)(rate_limit) if rate_limit > 0 else None,
            coalesce=coalesce,
        )

    if backend == "redis":
        # Only needed in redis mode, so memory mode works without it installed
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        from scheduler.redis_backend import RedisFairScheduler, RedisJobStore, RedisRateLimiter, RedisPacedRateLimiter

        
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "3")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "3")),
            retry=Retry(NoBackoff(), 0),
        )
        prefix = os.getenv("SCHEDULER_KEY_PREFIX", "sched:")

        return SchedulerWorker(
            inference_fn=inference_fn,
            scheduler=RedisFairScheduler(client, prefix),
            job_store=RedisJobStore(client, prefix),
            num_workers=num_workers,
            rate_limiter=(
                (RedisPacedRateLimiter if rate_mode == "paced" else RedisRateLimiter)(client, rate_limit, prefix=prefix)
                if rate_limit > 0 else None
            ),
            coalesce=coalesce,
        )

    raise ValueError(
        f"Unknown SCHEDULER_BACKEND {backend!r}, expected 'memory' or 'redis'"
    )
