"""Shows how a gateway request handler uses the cache.

Run Redis first:   docker compose up -d
Then:              pip install -r requirements.txt && python example.py
"""
import asyncio

from redis.asyncio import Redis

from caching.cache import ResponseCache
from caching.config import CacheConfig


async def call_llm(prompt: str, model: str) -> tuple[str, int]:
    """Stand-in for the external LLM API. Returns (response, tokens used)."""
    await asyncio.sleep(0.5)
    return f"[{model}] answer to: {prompt}", 120


async def handle_request(cache: ResponseCache, prompt: str, team_id: str, model: str) -> str:
    result = await cache.lookup(prompt, team_id, model)
    print(f"{'HIT ' if result.hit else 'MISS'} {result.latency_ms:6.2f} ms  [{team_id}] {prompt!r}")

    if result.hit:
        return result.response

    response, tokens = await call_llm(prompt, model)
    await cache.store(prompt, response, team_id, model, usage_tokens=tokens, key=result.key)
    return response


async def main() -> None:
    cfg = CacheConfig()
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    cache = ResponseCache(redis, cfg)
    await cache.clear()

    requests = [
        ("backend", "How do I deploy the service to staging?"),
        ("backend", "how do I deploy the service to staging"),   # hit: same after normalising
        ("backend", "What are the steps to deploy to staging?"),  # miss: different wording
        ("marketing", "How do I deploy the service to staging?"), # miss: other team
        ("backend", "How do I deploy the service to staging?"),   # hit
    ]
    for team, prompt in requests:
        await handle_request(cache, prompt, team, "gpt-4o-mini")

    print(await cache.stats())
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())