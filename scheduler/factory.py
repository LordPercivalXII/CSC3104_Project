"""
Builds the SchedulerWorker from environment variables.

    SCHEDULER_BACKEND     "memory" (default) or "redis"
    REDIS_URL             Redis to use in redis mode (same variable as the cache)
    SCHEDULER_KEY_PREFIX  prefix for the scheduler's Redis keys (default "sched:")
    SCHEDULER_WORKERS     requests this pod processes at once (default 1)
"""
import os

from scheduler.scheduler_worker import SchedulerWorker


def create_scheduler_worker(inference_fn=None):
    backend = os.getenv("SCHEDULER_BACKEND", "memory").lower()
    num_workers = int(os.getenv("SCHEDULER_WORKERS", "1"))

    if backend == "memory":
        return SchedulerWorker(inference_fn=inference_fn, num_workers=num_workers)

    if backend == "redis":
        # Only needed in redis mode, so memory mode works without it installed
        import redis

        from scheduler.redis_backend import RedisFairScheduler, RedisJobStore

        # Connects lazily, so importing main.py doesn't need Redis running
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        prefix = os.getenv("SCHEDULER_KEY_PREFIX", "sched:")

        return SchedulerWorker(
            inference_fn=inference_fn,
            scheduler=RedisFairScheduler(client, prefix),
            job_store=RedisJobStore(client, prefix),
            num_workers=num_workers,
        )

    raise ValueError(
        f"Unknown SCHEDULER_BACKEND {backend!r}, expected 'memory' or 'redis'"
    )