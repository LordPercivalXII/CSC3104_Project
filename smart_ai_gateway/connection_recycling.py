"""
Close long-lived client connections so new gateway replicas receive traffic.

A Kubernetes Service balances connections, not requests: kube-proxy picks a pod when a TCP
connection opens, and every request on that keep-alive connection goes to the same pod. When
the autoscaler adds replicas, clients that are already connected never reach them. In a
20-user load test, three new pods received no requests at all.

This middleware answers with "Connection: close" once a connection is older than
max_age_seconds. The client finishes that request normally, opens a new connection for the
next one, and the Service can route it to any replica, including new ones. One extra TCP
handshake per connection per max_age_seconds is the only cost.

Set CONNECTION_MAX_AGE_SECONDS (default 30); 0 turns this off.
"""
import os
import threading
from time import monotonic

DEFAULT_MAX_AGE_SECONDS = 30.0


def max_age_from_env() -> float:
    value = float(os.getenv("CONNECTION_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
    if value < 0:
        raise ValueError("CONNECTION_MAX_AGE_SECONDS cannot be negative")
    return value


class ConnectionRecyclingMiddleware:
    """Pure ASGI middleware, so streaming responses and background tasks are unaffected."""

    def __init__(self, app, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, clock=monotonic):
        self.app = app
        self.max_age = max_age_seconds
        self.clock = clock
        # (client address, server address) -> when this server first saw that connection.
        # The client's address and port identify one TCP connection while it is open.
        self.first_seen: dict = {}
        self.lock = threading.Lock()
        self._next_prune = 0.0

    async def __call__(self, scope, receive, send):
        client = scope.get("client") if scope["type"] == "http" else None
        if not client or self.max_age <= 0:
            await self.app(scope, receive, send)
            return

        key = (tuple(client), tuple(scope.get("server") or ()))
        now = self.clock()
        with self.lock:
            first = self.first_seen.setdefault(key, now)
            close = now - first >= self.max_age
            if close:
                self.first_seen.pop(key, None)
            if now >= self._next_prune:
                # Forget connections the client closed itself (they never reach max_age here)
                stale = now - 10 * self.max_age
                self.first_seen = {k: t for k, t in self.first_seen.items() if t >= stale}
                self._next_prune = now + self.max_age

        if not close:
            await self.app(scope, receive, send)
            return

        async def send_with_close(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"connection"]
                message = {**message, "headers": headers + [(b"connection", b"close")]}
            await send(message)

        await self.app(scope, receive, send_with_close)
