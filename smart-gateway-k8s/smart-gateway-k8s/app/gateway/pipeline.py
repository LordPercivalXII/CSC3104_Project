"""Request pipeline that ties the three components together.

    prompt
      |-> 1. Redis exact-match cache      (caching/)              -> "redis_cache"
      |-> 2. Qdrant semantic cache        (smart_ai_gateway/)     -> "semantic_cache"
      |-> 3. LLM stage, optionally behind the fair-share queue
      |       (scheduler/)                                        -> "mock_llm"
      '-> 4. store the answer in whichever caches are enabled

Each cache tier is optional and degrades gracefully: if Redis or Qdrant is
down at startup the tier is switched off (see status()); if a call fails
mid-request it is logged and skipped. A cache problem never fails a request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from redis.asyncio import Redis

from caching.cache import LookupResult, ResponseCache
from caching.config import CacheConfig

from .settings import GatewaySettings

log = logging.getLogger("gateway")

# Must match the embedding model used by smart_ai_gateway/services/semantic_cache.py
# and the collection created by smart_ai_gateway/scripts/setup_qdrant.py.
EMBEDDING_DIM = 384
QDRANT_CONNECT_ATTEMPTS = 30


class Pipeline:
    def __init__(
        self,
        settings: GatewaySettings,
        scheduler_worker: Any,
        cache_config: Optional[CacheConfig] = None,
    ):
        self.settings = settings
        self.worker = scheduler_worker
        self.cfg = cache_config or CacheConfig()
        self.redis: Optional[Redis] = None
        self.exact: Optional[ResponseCache] = None
        # The semantic_cache module (generate_embedding / search_cache / store_cache).
        self.semantic: Any = None

    # ---- lifecycle -------------------------------------------------------

    async def startup(self) -> None:
        if self.settings.use_exact:
            await self._start_exact()
        if self.settings.use_semantic:
            await self._start_semantic()
        log.info("pipeline ready: %s", self.status())

    async def shutdown(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None
        if self.semantic is not None:
            try:
                await asyncio.to_thread(self.semantic.client.close)
            except Exception as exc:  # noqa: BLE001 - shutting down anyway
                log.debug("qdrant close failed: %s", exc)
            self.semantic = None
        self.exact = None

    async def _start_exact(self) -> None:
        client = Redis.from_url(self.cfg.redis_url, decode_responses=True)
        try:
            await client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis unavailable at %s, exact cache disabled: %s", self.cfg.redis_url, exc)
            await client.aclose()
            return
        self.redis = client
        self.exact = ResponseCache(client, self.cfg)

    async def _start_semantic(self) -> None:
        try:
            self.semantic = await asyncio.to_thread(self._load_semantic)
        except Exception as exc:  # noqa: BLE001
            log.warning("Semantic cache unavailable, tier disabled: %s", exc)

    def _load_semantic(self) -> Any:
        # Importing this module loads the embedding model and builds the Qdrant
        # client, so it is imported lazily and only when the tier is enabled.
        from qdrant_client import models

        from smart_ai_gateway.services import semantic_cache

        # The module hardcodes 0.90; apply the configured value (the knob swept in experiments).
        semantic_cache.SIMILARITY_THRESHOLD = self.cfg.similarity_threshold

        # Same collection smart_ai_gateway/scripts/setup_qdrant.py creates. Retry because
        # Qdrant may still be starting when the gateway comes up.
        client, name = semantic_cache.client, semantic_cache.COLLECTION_NAME
        for attempt in range(1, QDRANT_CONNECT_ATTEMPTS + 1):
            try:
                if not client.collection_exists(name):
                    client.create_collection(
                        collection_name=name,
                        vectors_config=models.VectorParams(
                            size=EMBEDDING_DIM, distance=models.Distance.COSINE
                        ),
                    )
                return semantic_cache
            except Exception as exc:  # noqa: BLE001
                if attempt == QDRANT_CONNECT_ATTEMPTS:
                    raise
                log.info("waiting for Qdrant (%s/%s): %s", attempt, QDRANT_CONNECT_ATTEMPTS, exc)
                time.sleep(1)
        return semantic_cache  # unreachable; keeps type checkers happy

    # ---- request handling ------------------------------------------------

    async def handle(self, prompt: str, user_id: str, team_id: str, model: str) -> dict:
        lookup: Optional[LookupResult] = None
        embedding = None

        # 1. exact match
        if self.exact is not None:
            try:
                lookup = await self.exact.lookup(prompt, team_id, model)
            except Exception as exc:  # noqa: BLE001
                log.warning("exact cache lookup failed: %s", exc)
            else:
                if lookup.hit:
                    return {
                        "response": lookup.response,
                        "source": "redis_cache",
                        "similarity_score": None,
                        "cache_hits": lookup.hits,
                        "latency_ms": round(lookup.latency_ms, 2),
                    }

        # 2. semantic match (the embedding is reused when storing a miss)
        if self.semantic is not None:
            try:
                embedding = await asyncio.to_thread(self.semantic.generate_embedding, prompt)
                hit = await asyncio.to_thread(self.semantic.search_cache, embedding)
            except Exception as exc:  # noqa: BLE001
                log.warning("semantic cache lookup failed: %s", exc)
            else:
                if hit is not None:
                    return {
                        "response": hit["response"],
                        "source": "semantic_cache",
                        "similarity_score": hit["similarity_score"],
                        "matched_prompt": hit.get("matched_prompt"),
                    }

        # 3. miss: ask the LLM
        response = await self._call_llm(prompt, user_id)

        # 4. remember it
        await self._store(prompt, response, team_id, model, lookup, embedding)

        return {"response": response, "source": "mock_llm", "similarity_score": None}

    async def _call_llm(self, prompt: str, user_id: str) -> str:
        if not self.settings.fair_share_enabled:
            return f"You asked: {prompt}"

        future = self.worker.submit(user_id, prompt)
        # shield(): if this request is cancelled, the worker's own future must not be
        # cancelled with it. SchedulerWorker can't cope with a cancelled future, and
        # a single worker thread is shared by every user.
        result = await asyncio.shield(asyncio.wrap_future(future))
        return result["response"]

    async def _store(
        self,
        prompt: str,
        response: str,
        team_id: str,
        model: str,
        lookup: Optional[LookupResult],
        embedding: Optional[list],
    ) -> None:
        if self.exact is not None:
            try:
                # usage_tokens stays unset until a real LLM reports it, so the
                # `tokens_saved` counter is honest (0) rather than invented.
                await self.exact.store(
                    prompt, response, team_id, model, key=lookup.key if lookup else None
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("exact cache store failed: %s", exc)

        if self.semantic is not None and embedding is not None:
            try:
                await asyncio.to_thread(
                    self.semantic.store_cache,
                    prompt=prompt,
                    response=response,
                    embedding=embedding,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("semantic cache store failed: %s", exc)

    # ---- introspection ---------------------------------------------------

    def status(self) -> dict:
        return {
            "cache_mode": self.settings.cache_mode,
            "exact_cache_active": self.exact is not None,
            "semantic_cache_active": self.semantic is not None,
            "fair_share_enabled": self.settings.fair_share_enabled,
        }

    async def cache_stats(self) -> dict:
        """Counters for the exact tier only (the semantic tier keeps none)."""
        if self.exact is None:
            return {"exact_cache_active": False}
        return {"exact_cache_active": True, **await self.exact.stats()}
