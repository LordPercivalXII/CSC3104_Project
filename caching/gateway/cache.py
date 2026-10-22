"""Redis response cache for the inference gateway (exact-match only).

Each cached answer is one Redis hash:

    cache:{team_id}:{model}:{sha256(normalised prompt + params)}
        prompt        original prompt text
        response      LLM response
        created_at    unix time stored
        last_hit_at   unix time of latest hit
        hits          number of times served from cache
        usage_tokens  tokens the original LLM call cost (used to estimate savings)

Global counters for experiments live in one hash, `cache:stats`
(hits, misses, stores, tokens_saved).

Entries are scoped to a team, so one team's answers never reach another, and to
a model, so switching models doesn't serve old outputs. The gateway replicas
hold no state of their own: any replica can read or write any entry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from redis.asyncio import Redis

from .config import CacheConfig
from .normalise import request_hash


@dataclass
class LookupResult:
    hit: bool
    response: Optional[str]
    key: str          # the cache key; pass it to store() after a miss
    hits: int         # how many times this entry has now been served (0 on a miss)
    latency_ms: float


class ResponseCache:
    def __init__(self, redis: Redis, config: CacheConfig | None = None):
        # Use a client created with decode_responses=True.
        self.r = redis
        self.cfg = config or CacheConfig()
        self.stats_key = f"{self.cfg.key_prefix}stats"

    def make_key(self, prompt: str, team_id: str, model: str, params: dict | None = None) -> str:
        digest = request_hash(prompt, params, self.cfg.normalise_prompts)
        return f"{self.cfg.key_prefix}{team_id}:{model}:{digest}"

    async def lookup(
        self, prompt: str, team_id: str, model: str, params: dict | None = None
    ) -> LookupResult:
        start = time.perf_counter()
        key = self.make_key(prompt, team_id, model, params)

        response, usage = await self.r.hmget(key, "response", "usage_tokens")

        if response is None:
            await self.r.hincrby(self.stats_key, "misses", 1)
            return LookupResult(False, None, key, 0, (time.perf_counter() - start) * 1000)

        # Record the hit and the tokens it saved in one round trip.
        pipe = self.r.pipeline(transaction=False)
        pipe.hincrby(key, "hits", 1)
        pipe.hset(key, "last_hit_at", int(time.time()))
        pipe.hincrby(self.stats_key, "hits", 1)
        if usage is not None:
            pipe.hincrby(self.stats_key, "tokens_saved", int(usage))
        if self.cfg.refresh_ttl_on_hit and self.cfg.ttl_seconds:
            pipe.expire(key, self.cfg.ttl_seconds)
        hits = (await pipe.execute())[0]

        return LookupResult(True, response, key, hits, (time.perf_counter() - start) * 1000)

    async def store(
        self,
        prompt: str,
        response: str,
        team_id: str,
        model: str,
        params: dict | None = None,
        usage_tokens: int | None = None,
        key: str | None = None,
    ) -> str:
        """Cache an LLM response. Pass `key` from the miss's LookupResult to skip re-hashing."""
        key = key or self.make_key(prompt, team_id, model, params)
        now = int(time.time())
        mapping = {
            "prompt": prompt,
            "response": response,
            "created_at": now,
            "last_hit_at": now,
            "hits": 0,
        }
        if usage_tokens is not None:
            mapping["usage_tokens"] = usage_tokens

        # MULTI/EXEC so the entry never exists without its TTL.
        pipe = self.r.pipeline(transaction=True)
        pipe.delete(key)  # replace cleanly if a concurrent request stored it first
        pipe.hset(key, mapping=mapping)
        if self.cfg.ttl_seconds:
            pipe.expire(key, self.cfg.ttl_seconds)
        pipe.hincrby(self.stats_key, "stores", 1)
        await pipe.execute()
        return key

    async def invalidate(self, prompt: str, team_id: str, model: str, params: dict | None = None) -> bool:
        """Drop one cached answer, e.g. after a user reports it as wrong or outdated."""
        return bool(await self.r.delete(self.make_key(prompt, team_id, model, params)))

    async def clear(self, team_id: str | None = None, reset_stats: bool = True) -> int:
        """Delete cached entries (all, or one team's). Used to reset between experiment runs."""
        pattern = f"{self.cfg.key_prefix}{team_id if team_id else '*'}:*"
        deleted, batch = 0, []
        async for key in self.r.scan_iter(match=pattern, count=500):
            if key == self.stats_key:
                continue
            batch.append(key)
            if len(batch) >= 500:
                deleted += await self.r.delete(*batch)
                batch.clear()
        if batch:
            deleted += await self.r.delete(*batch)
        if reset_stats:
            await self.r.delete(self.stats_key)
        return deleted

    async def stats(self) -> dict:
        raw = await self.r.hgetall(self.stats_key)
        s = {k: int(raw.get(k, 0)) for k in ("hits", "misses", "stores", "tokens_saved")}
        total = s["hits"] + s["misses"]
        s["hit_rate"] = round(s["hits"] / total, 4) if total else 0.0
        return s
