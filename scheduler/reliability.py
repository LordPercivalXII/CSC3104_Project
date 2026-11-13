"""Bounded jobs, durable results and fenced processing leases.

Recovery is at least once: a provider call whose outcome was lost can be repeated.
Terminal results are retained to recover from missed result notifications.
"""
import copy
import json
import threading
import time
import uuid

from scheduler.errors import QueueFullError, SchedulerTimeoutError, SchedulerUnavailableError


def encode_message(message):
    message = dict(message)
    if isinstance(message.get("error"), BaseException):
        error = message["error"]
        message.update(error=str(error), error_type=type(error).__name__,
                       rate_limited=bool(getattr(error, "rate_limited", False)))
    return message


def decode_message(message):
    if isinstance(message, str):
        message = json.loads(message)
    message = dict(message)
    if isinstance(message.get("error"), str):
        from scheduler.redis_backend import RemoteInferenceError
        classes = {cls.__name__: cls for cls in (SchedulerTimeoutError, SchedulerUnavailableError, QueueFullError)}
        cls = classes.get(message.get("error_type"))
        message["error"] = cls(message["error"]) if cls else RemoteInferenceError(
            message["error"], message.get("rate_limited", False))
    return message


class RedisCoordinator:
    def __init__(self, scheduler, store, config):
        from scheduler.redis_scripts import RELIABLE_SCRIPT
        self.scheduler, self.store, self.config = scheduler, store, config
        self.prefix = scheduler.pending_key[:-len("pending")]
        self.redis = scheduler.redis
        self.script = self.redis.register_script(RELIABLE_SCRIPT)

    def run(self, operation, args):
        return self.script(args=[self.prefix, operation, json.dumps(args), json.dumps(self.config)])

    def submit(self, request):
        leader = self.run("submit", {"request": request})
        if leader == "!full":
            raise QueueFullError("The shared request queue is full")
        return leader or None

    def take(self):
        value = self.run("take", {"token": uuid.uuid4().hex})
        return (value[0], json.loads(value[1])) if value else None

    def complete(self, request, message):
        return bool(self.run("complete", {"request": request, "message": encode_message(message)}))

    def renew(self, request):
        return self.run("renew", {"id": request["request_id"], "token": request["lease_token"]})

    def maintain(self):
        self.run("maintain", {})

    def cancel(self, request_id):
        return self.run("cancel", {"id": request_id})

    def result(self, request_id):
        value = self.redis.hget(self.store.job_prefix + request_id, "result")
        return decode_message(value) if value else None

    def results(self, ids):
        if not ids:
            return []
        with self.redis.pipeline(transaction=False) as pipe:
            for rid in ids:
                pipe.hget(self.store.job_prefix + rid, "result")
            return [decode_message(value) if value else None for value in pipe.execute()]


class MemoryCoordinator:
    def __init__(self, scheduler, store, config):
        self.scheduler, self.store, self.config = scheduler, store, config
        self.lock = threading.RLock()
        self.states, self.inflight, self.waiters = {}, {}, {}

    def _status(self, request_id, status):
        self.store.set_status(request_id, status)
        self.states[request_id]["status"] = status

    def _deliver(self, request, message):
        state = self.states.get(request["request_id"])
        if state is None or state["status"] == "CANCELLED":
            return
        state["result"] = message
        state["finished"] = time.monotonic()
        self._status(request["request_id"], "FAILED" if "error" in message else "COMPLETED")
        try:
            self.store.publish_result(request["reply_to"], message)
        except Exception:
            # A notification failure does not lose the retained result.
            pass

    def _finish(self, request, message):
        self._deliver(request, message)
        for waiter in self.waiters.pop(request["request_id"], {}).values():
            reply = copy.deepcopy(message)
            reply["request_id"] = waiter["request_id"]
            if "response" in reply:
                reply["response"].update(request_id=waiter["request_id"], user_id=waiter["user_id"], coalesced=True)
            self._deliver(waiter, reply)
        key = request.get("dedup_key")
        if key and self.inflight.get(key) == request["request_id"]:
            self.inflight.pop(key, None)

    def submit(self, request):
        with self.lock:
            self.maintain()
            active = [s for s in self.states.values() if s["status"] in ("PENDING", "PROCESSING")]
            user = request["user_id"]
            if (len(active) >= self.config["max_pending"] or self.scheduler.pending_count() >= self.config["max_pending"]
                    or sum(s["request"]["user_id"] == user for s in active) >= self.config["max_per_user"]):
                raise QueueFullError("The request queue is full")
            request = dict(request, deadline=time.monotonic() + self.config["timeout"])
            rid, key = request["request_id"], request.get("dedup_key")
            self.store.create(rid, user)
            self.states[rid] = {"request": request, "status": "PENDING", "attempts": 0}
            leader = self.inflight.get(key) if key else None
            try:
                if leader:
                    self.states[rid]["leader"] = leader
                    self.waiters.setdefault(leader, {})[rid] = request
                    if self.states[leader]["status"] == "PENDING":
                        self.scheduler.add_request(user, dict(request, leader_id=leader))
                    return leader
                if key:
                    self.inflight[key] = rid
                self.scheduler.add_request(user, request)
                return None
            except Exception:
                self.cancel(rid)
                raise

    def take(self):
        with self.lock:
            for _ in range(32):
                queued = self.scheduler.get_next_request()
                if queued is None:
                    return None
                user, marker = queued
                rid = marker.get("leader_id", marker["request_id"])
                state = self.states.get(rid)
                if state is None or state["status"] != "PENDING":
                    continue
                request = state["request"]
                if self.store.get_status(rid) == "CANCELLED":
                    self.cancel(rid)
                    continue
                if request["deadline"] <= time.monotonic():
                    self._finish(request, {"request_id": rid, "error": SchedulerTimeoutError("Request deadline exceeded")})
                    continue
                if self.store.get_status(rid) is None:
                    self.store.create(rid, request["user_id"])
                    self._finish(request, {"request_id": rid, "error": SchedulerUnavailableError("Queued job state was lost")})
                    continue
                self._status(rid, "PROCESSING")
                state["attempts"] += 1
                request["lease_token"] = uuid.uuid4().hex
                state["lease"] = time.monotonic() + self.config["lease"]
                return user, dict(request)
            return None

    def complete(self, request, message):
        with self.lock:
            state = self.states.get(request["request_id"])
            if not state or state["status"] != "PROCESSING" or state["request"].get("lease_token") != request["lease_token"]:
                return False
            if request["deadline"] <= time.monotonic():
                self._finish(request, {"request_id": request["request_id"], "error": SchedulerTimeoutError("Request deadline exceeded")})
                return False
            self._finish(request, message)
            return True

    def renew(self, request):
        with self.lock:
            state = self.states.get(request["request_id"])
            if not state or state["status"] != "PROCESSING" or state["request"].get("lease_token") != request["lease_token"]:
                return False
            state["lease"] = time.monotonic() + self.config["lease"]
            return True

    def maintain(self):
        with self.lock:
            now = time.monotonic()
            for rid, state in list(self.states.items()):
                if state["status"] not in ("PENDING", "PROCESSING"):
                    if now - state.get("finished", now) >= self.config["retention"]:
                        self.states.pop(rid, None)
                        with self.store.lock:
                            self.store.jobs.pop(rid, None)
                    continue
                request = state["request"]
                if request["deadline"] <= now:
                    leader = state.get("leader")
                    if leader:
                        self.waiters.get(leader, {}).pop(rid, None)
                    self._finish(request, {"request_id": rid, "error": SchedulerTimeoutError("Request deadline exceeded")})
                elif state["status"] == "PROCESSING" and state["lease"] <= now:
                    if state["attempts"] >= self.config["attempts"]:
                        self._finish(request, {"request_id": rid, "error": SchedulerUnavailableError("Processing lease expired")})
                    else:
                        self._status(rid, "PENDING")
                        request.pop("lease_token", None)
                        self.scheduler.add_request(request["user_id"], dict(request))
            terminal = [rid for rid,s in self.states.items() if s["status"] not in ("PENDING", "PROCESSING")]
            for rid in terminal[:-10000]:
                self.states.pop(rid, None)
                with self.store.lock:
                    self.store.jobs.pop(rid, None)
            def keep(marker):
                state = self.states.get(marker["request_id"])
                leader = self.states.get(marker.get("leader_id"))
                return (state is None or state["status"] == "PENDING") and (not marker.get("leader_id") or leader and leader["status"] == "PENDING")
            self.scheduler.prune(keep)

    def cancel(self, request_id):
        with self.lock:
            state = self.states.get(request_id)
            if not state or state["status"] not in ("PENDING", "PROCESSING"):
                return False
            if not state.get("leader") and self.waiters.get(request_id):
                state["detached"] = True
                return False
            self._status(request_id, "CANCELLED")
            state["finished"] = time.monotonic()
            request = state["request"]
            leader = state.get("leader")
            if leader:
                self.waiters.get(leader, {}).pop(request_id, None)
                if not self.waiters.get(leader) and self.states.get(leader, {}).get("detached"):
                    self.cancel(leader)
            key = request.get("dedup_key")
            if key and self.inflight.get(key) == request_id:
                self.inflight.pop(key, None)
            return True

    def result(self, request_id):
        with self.lock:
            return self.states.get(request_id, {}).get("result")

    def results(self, ids):
        return [self.result(rid) for rid in ids]
