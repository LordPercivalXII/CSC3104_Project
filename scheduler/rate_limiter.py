"""
In-memory rate limiter for LLM calls inside one process.

RedisRateLimiter (redis_backend.py) has the same methods and shares one
limit across all pods.
"""
import threading
import time
from collections import deque


class MemoryRateLimiter:
    """Sliding window: at most `limit` calls start in any `window_seconds`."""

    def __init__(self, limit, window_seconds=60):
        self.limit = limit
        self.window_seconds = window_seconds

        # Start times of the calls inside the current window
        self.calls = deque()
        self.lock = threading.Lock()

    def try_acquire(self):
        """Take a slot if one is free.

        Returns 0 if a slot was taken, otherwise the seconds until one frees up.
        """
        with self.lock:
            now = time.monotonic()

            # Forget calls that have left the window
            while self.calls and self.calls[0] <= now - self.window_seconds:
                self.calls.popleft()

            if len(self.calls) < self.limit:
                self.calls.append(now)
                return 0

            return self.calls[0] + self.window_seconds - now

    def release(self):
        """Give back a slot that was taken but not used for an LLM call."""
        with self.lock:
            if self.calls:
                self.calls.pop()
