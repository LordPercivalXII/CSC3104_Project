"""
Redis versions of the fair queue and job store, shared by all gateway pods.

Keys: sched:queue:<user>, sched:rotation, sched:pending,
      sched:job:<request_id>, sched:results:<instance>, sched:ratelimit

Queue keys never expire, so Redis must use maxmemory-policy volatile-lru.
The client must be created with decode_responses=True.
"""
import json
import uuid

JOB_TTL_SECONDS = 3600
RESULT_TTL_SECONDS = 300


# Lua scripts run atomically in Redis, replacing threading.Lock across pods.

ENQUEUE_SCRIPT = """
-- KEYS: user's queue, rotation, pending counter
-- ARGV: user id, request JSON
if redis.call('RPUSH', KEYS[1], ARGV[2]) == 1 then
    redis.call('RPUSH', KEYS[2], ARGV[1])
end
redis.call('INCR', KEYS[3])
"""

DEQUEUE_SCRIPT = """
-- KEYS: rotation, pending counter
-- ARGV: prefix of the user queue keys
local user = redis.call('LPOP', KEYS[1])
if not user then
    return nil
end

local queue = ARGV[1] .. user
local request = redis.call('LPOP', queue)
if not request then
    return nil
end

if redis.call('LLEN', queue) > 0 then
    redis.call('RPUSH', KEYS[1], user)
end
redis.call('DECR', KEYS[2])
return {user, request}
"""

# Only changes the status if it matches the expected one
TRANSITION_SCRIPT = """
-- KEYS: job hash
-- ARGV: expected status, new status, ttl
if redis.call('HGET', KEYS[1], 'status') == ARGV[1] then
    redis.call('HSET', KEYS[1], 'status', ARGV[2])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    return 1
end
return 0
"""


RATE_LIMIT_SCRIPT = """
-- KEYS: sorted set of recent call times
-- ARGV: limit, window (ms), unique id for this call
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local window = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[1]) then
    redis.call('ZADD', KEYS[1], now, ARGV[3])
    redis.call('PEXPIRE', KEYS[1], window)
    return 0
end

local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return tonumber(oldest[2]) + window - now
"""


class RemoteInferenceError(RuntimeError):
    """The LLM call failed on the pod that processed the request."""

    def __init__(self, message, rate_limited=False):
        super().__init__(message)
        # Kept from the original error, so the API can still answer 429s with Retry-After
        self.rate_limited = rate_limited


class RedisFairScheduler:
    """Same methods as FairShareScheduler, stored in Redis."""

    def __init__(self, redis_client, prefix="sched:"):
        self.redis = redis_client
        self.queue_prefix = f"{prefix}queue:"
        self.rotation_key = f"{prefix}rotation"
        self.pending_key = f"{prefix}pending"

        self._enqueue = redis_client.register_script(ENQUEUE_SCRIPT)
        self._dequeue = redis_client.register_script(DEQUEUE_SCRIPT)

    def add_request(self, user_id, request):
        self._enqueue(
            keys=[self.queue_prefix + user_id, self.rotation_key, self.pending_key],
            args=[user_id, json.dumps(request)],
        )

    def get_next_request(self):
        result = self._dequeue(
            keys=[self.rotation_key, self.pending_key],
            args=[self.queue_prefix],
        )
        if result is None:
            return None

        user_id, request = result
        return user_id, json.loads(request)

    def pending_count(self):
        return int(self.redis.get(self.pending_key) or 0)


class RedisJobStore:
    """Same methods as MemoryJobStore, shared by all pods."""

    def __init__(self, redis_client, prefix="sched:"):
        self.redis = redis_client
        self.job_prefix = f"{prefix}job:"
        self.results_prefix = f"{prefix}results:"

        self._transition = redis_client.register_script(TRANSITION_SCRIPT)

    def create(self, request_id, user_id):
        key = self.job_prefix + request_id
        with self.redis.pipeline() as pipe:
            pipe.hset(key, mapping={"user_id": user_id, "status": "PENDING"})
            pipe.expire(key, JOB_TTL_SECONDS)
            pipe.execute()

    def claim(self, request_id):
        """PENDING -> PROCESSING. False if the caller already cancelled."""
        return self._change(request_id, "PENDING", "PROCESSING")

    def cancel(self, request_id):
        """PENDING -> CANCELLED. False if it is already being processed."""
        return self._change(request_id, "PENDING", "CANCELLED")

    def _change(self, request_id, expected, new):
        changed = self._transition(
            keys=[self.job_prefix + request_id],
            args=[expected, new, JOB_TTL_SECONDS],
        )
        return changed == 1

    def set_status(self, request_id, status):
        key = self.job_prefix + request_id
        with self.redis.pipeline() as pipe:
            pipe.hset(key, "status", status)
            pipe.expire(key, JOB_TTL_SECONDS)
            pipe.execute()

    def get_status(self, request_id):
        return self.redis.hget(self.job_prefix + request_id, "status")

    def publish_result(self, reply_to, message):
        # Exceptions can't be stored as JSON, so send the error as text
        message = dict(message)
        error = message.pop("error", None)
        if error is not None:
            message["error"] = f"{type(error).__name__}: {error}"
            message["rate_limited"] = bool(getattr(error, "rate_limited", False))

        key = self.results_prefix + reply_to
        with self.redis.pipeline() as pipe:
            pipe.rpush(key, json.dumps(message))
            pipe.expire(key, RESULT_TTL_SECONDS)
            pipe.execute()

    def next_result(self, reply_to, timeout):
        """Wait up to `timeout` seconds for a result addressed to this pod."""
        item = self.redis.blpop([self.results_prefix + reply_to], timeout=timeout)
        if item is None:
            return None

        message = json.loads(item[1])
        if "error" in message:
            message["error"] = RemoteInferenceError(
                message["error"], message.pop("rate_limited", False)
            )
        return message


class RedisRateLimiter:
    """Same methods as MemoryRateLimiter, one limit shared by all pods."""

    def __init__(self, redis_client, limit, window_seconds=60, prefix="sched:"):
        self.limit = limit
        self.window_ms = int(window_seconds * 1000)
        self.key = f"{prefix}ratelimit"
        self.redis = redis_client

        self._acquire = redis_client.register_script(RATE_LIMIT_SCRIPT)

    def try_acquire(self):
        """Take a slot if one is free.

        Returns 0 if a slot was taken, otherwise the seconds until one frees up.
        """
        wait_ms = self._acquire(
            keys=[self.key],
            args=[self.limit, self.window_ms, uuid.uuid4().hex],
        )
        return int(wait_ms) / 1000

    def release(self):
        """Give back a slot that was taken but not used for an LLM call.

        Removes the newest slot. Slots are interchangeable, so this keeps the
        count right even if another pod took one a moment later.
        """
        self.redis.zpopmax(self.key)
