"""
Redis versions of the fair queue and job store, shared by all gateway pods.

Keys: sched:queue:<user>, sched:rotation, sched:pending,
      sched:job:<request_id>, sched:results:<instance>, sched:ratelimit,
      sched:inflight:<question hash>, sched:waiters:<leader request_id>

Queue keys never expire, so Redis must use maxmemory-policy volatile-lru.
The client must be created with decode_responses=True.
"""
import json
import uuid

JOB_TTL_SECONDS = 3600
RESULT_TTL_SECONDS = 300

# How long a question counts as "being answered". Only matters if the pod
# making the call dies: duplicates arriving after this lead a new call.
INFLIGHT_TTL_SECONDS = 300


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


# Request coalescing: identical questions asked at the same time share one
# LLM call. The first becomes the leader; the rest wait for its answer.
JOIN_OR_LEAD_SCRIPT = """
-- KEYS: in-flight key for this question
-- ARGV: request id, waiter JSON, in-flight ttl, waiters key prefix, waiters ttl
local leader = redis.call('GET', KEYS[1])
if not leader then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
    return false
end

local waiters = ARGV[4] .. leader
redis.call('RPUSH', waiters, ARGV[2])
redis.call('EXPIRE', waiters, ARGV[5])
return leader
"""

FINISH_SCRIPT = """
-- KEYS: in-flight key, this leader's waiters list
-- ARGV: leader request id
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
end
local waiters = redis.call('LRANGE', KEYS[2], 0, -1)
redis.call('DEL', KEYS[2])
return waiters
"""

# A leader is only cancelled if nobody is waiting for its answer
CANCEL_LEADER_SCRIPT = """
-- KEYS: job hash, in-flight key, this leader's waiters list
-- ARGV: leader request id, job ttl
if redis.call('LLEN', KEYS[3]) > 0 then
    return 0
end
if redis.call('HGET', KEYS[1], 'status') ~= 'PENDING' then
    return 0
end
redis.call('HSET', KEYS[1], 'status', 'CANCELLED')
redis.call('EXPIRE', KEYS[1], ARGV[2])
if redis.call('GET', KEYS[2]) == ARGV[1] then
    redis.call('DEL', KEYS[2])
end
return 1
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
        self.inflight_prefix = f"{prefix}inflight:"
        self.waiters_prefix = f"{prefix}waiters:"

        self._transition = redis_client.register_script(TRANSITION_SCRIPT)
        self._join_or_lead = redis_client.register_script(JOIN_OR_LEAD_SCRIPT)
        self._finish = redis_client.register_script(FINISH_SCRIPT)
        self._cancel_leader = redis_client.register_script(CANCEL_LEADER_SCRIPT)

    def create(self, request_id, user_id):
        key = self.job_prefix + request_id
        with self.redis.pipeline() as pipe:
            pipe.hset(key, mapping={"user_id": user_id, "status": "PENDING"})
            pipe.expire(key, JOB_TTL_SECONDS)
            pipe.execute()

    def claim(self, request_id):
        """PENDING -> PROCESSING. False if the caller already cancelled."""
        return self._change(request_id, "PENDING", "PROCESSING")

    def cancel(self, request_id, dedup_key=None):
        """PENDING -> CANCELLED. False if it is already being processed.

        A leader (dedup_key given) is not cancelled while others wait for its
        answer. Otherwise it stops leading, so the next duplicate leads itself.
        """
        if dedup_key is None:
            return self._change(request_id, "PENDING", "CANCELLED")

        cancelled = self._cancel_leader(
            keys=[
                self.job_prefix + request_id,
                self.inflight_prefix + dedup_key,
                self.waiters_prefix + request_id,
            ],
            args=[request_id, JOB_TTL_SECONDS],
        )
        return cancelled == 1

    def _change(self, request_id, expected, new):
        changed = self._transition(
            keys=[self.job_prefix + request_id],
            args=[expected, new, JOB_TTL_SECONDS],
        )
        return changed == 1

    def join_or_lead(self, dedup_key, request_id, waiter):
        """Lead the LLM call for dedup_key, or wait for the current leader.

        Returns None if this request leads, otherwise the leader's request ID.
        """
        return self._join_or_lead(
            keys=[self.inflight_prefix + dedup_key],
            args=[request_id, json.dumps(waiter), INFLIGHT_TTL_SECONDS,
                  self.waiters_prefix, JOB_TTL_SECONDS],
        )

    def finish(self, dedup_key, request_id):
        """Stop leading and return the requests waiting for this answer."""
        waiters = self._finish(
            keys=[self.inflight_prefix + dedup_key, self.waiters_prefix + request_id],
            args=[request_id],
        )
        return [json.loads(waiter) for waiter in waiters]

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

        from scheduler.reliability import decode_message
        return decode_message(item[1])


class RedisRateLimiter:
    """Same methods as MemoryRateLimiter, one limit shared by all pods."""

    def __init__(self, redis_client, limit, window_seconds=60, prefix="sched:"):
        self.limit = limit
        self.window_ms = int(window_seconds * 1000)
        self.key = f"{prefix}ratelimit"
        self.redis = redis_client

        self._acquire = redis_client.register_script(RATE_LIMIT_SCRIPT)
        import threading
        self.reservation = threading.local()

    def try_acquire(self):
        """Take a slot if one is free.

        Returns 0 if a slot was taken, otherwise the seconds until one frees up.
        """
        token = uuid.uuid4().hex
        wait_ms = self._acquire(
            keys=[self.key],
            args=[self.limit, self.window_ms, token],
        )
        if int(wait_ms) == 0:
            self.reservation.value = token
        return int(wait_ms) / 1000

    def release(self):
        """Give back a slot that was taken but not used for an LLM call.

        Removes this thread's reservation, preserving another pod's newer slot.
        """
        token = getattr(self.reservation, "value", None)
        if token:
            self.redis.zrem(self.key, token)
            self.reservation.value = None


class RedisPacedRateLimiter:
    """Smooth admission on Redis server time, shared by every gateway."""

    def __init__(self, redis_client, limit, window_seconds=60, prefix="sched:"):
        import threading
        self.redis, self.key = redis_client, prefix + "pacedlimit"
        self.interval_ms = max(1, int(window_seconds * 1000 / limit))
        self.reservation = threading.local()
        self._acquire = redis_client.register_script('''
            local t = redis.call('TIME')
            local now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
            local next_at = tonumber(redis.call('HGET', KEYS[1], 'next') or '0')
            if next_at > now then return math.ceil(next_at - now) end
            redis.call('HSET', KEYS[1], 'next', now + tonumber(ARGV[1]), 'token', ARGV[2])
            redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1])) + 1000)
            return 0
        ''')
        self._release = redis_client.register_script('''
            if redis.call('HGET', KEYS[1], 'token') == ARGV[1] then return redis.call('DEL', KEYS[1]) end
            return 0
        ''')

    def try_acquire(self):
        token = uuid.uuid4().hex
        wait = int(self._acquire(keys=[self.key], args=[self.interval_ms, token])) / 1000
        if wait == 0:
            self.reservation.value = token
        return wait

    def release(self):
        token = getattr(self.reservation, "value", None)
        if token:
            self._release(keys=[self.key], args=[token])
            self.reservation.value = None
