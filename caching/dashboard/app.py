"""Web dashboard for viewing and managing the gateway's Redis response cache.

Run locally:    uvicorn dashboard.app:app --port 8080
Then open:      http://localhost:8080

Set DASHBOARD_TOKEN to require a token (sent as the X-Dashboard-Token header).
Cached responses may contain internal company information, so set a token
whenever the dashboard is reachable by anyone other than you.
"""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from redis.asyncio import Redis

from gateway.cache import ResponseCache
from gateway.config import CacheConfig

cfg = CacheConfig()
TOKEN = os.getenv("DASHBOARD_TOKEN", "")
STATIC = Path(__file__).parent / "static"
MAX_SCAN = 20_000  # stop scanning after this many keys per request so huge caches stay responsive

state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["r"] = Redis.from_url(cfg.redis_url, decode_responses=True)
    state["cache"] = ResponseCache(state["r"], cfg)
    yield
    await state["r"].aclose()


app = FastAPI(title="Gateway cache dashboard", lifespan=lifespan)


def check_token(x_dashboard_token: str = Header(default="")) -> None:
    if TOKEN and not secrets.compare_digest(x_dashboard_token, TOKEN):
        raise HTTPException(401, "Missing or wrong dashboard token")


def r() -> Redis:
    return state["r"]


def parse_key(key: str) -> dict:
    """cache:{team}:{model}:{hash} -> parts. Model names may themselves contain ':'."""
    body = key[len(cfg.key_prefix):]
    team, rest = body.split(":", 1)
    model, digest = rest.rsplit(":", 1)
    return {"key": key, "team": team, "model": model, "hash": digest}


def require_entry_key(key: str) -> None:
    if not key.startswith(cfg.key_prefix) or key == state["cache"].stats_key or key.count(":") < 3:
        raise HTTPException(400, "Not a cache entry key")


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    await r().ping()
    return {"ok": True}


# ------------------------------------------------------------------ api

@app.get("/api/overview", dependencies=[Depends(check_token)])
async def overview():
    stats = await state["cache"].stats()
    mem = await r().info("memory")
    st = await r().info("stats")
    srv = await r().info("server")
    return {
        "cache": stats,
        "redis": {
            "version": srv.get("redis_version"),
            "total_keys": await r().dbsize(),
            "used_memory": mem.get("used_memory_human"),
            "max_memory": mem.get("maxmemory_human"),
            "eviction_policy": mem.get("maxmemory_policy"),
            "evicted_keys": st.get("evicted_keys", 0),
            "expired_keys": st.get("expired_keys", 0),
        },
        "config": {
            "ttl_seconds": cfg.ttl_seconds,
            "refresh_ttl_on_hit": cfg.refresh_ttl_on_hit,
            "normalise_prompts": cfg.normalise_prompts,
        },
    }


@app.get("/api/facets", dependencies=[Depends(check_token)])
async def facets():
    """Distinct teams and models present in the cache, for the filter menus."""
    teams: dict[str, int] = {}
    models: set[str] = set()
    seen = 0
    async for key in r().scan_iter(match=f"{cfg.key_prefix}*:*:*", count=1000):
        if key == state["cache"].stats_key:
            continue
        p = parse_key(key)
        teams[p["team"]] = teams.get(p["team"], 0) + 1
        models.add(p["model"])
        seen += 1
        if seen >= MAX_SCAN:
            break
    return {"teams": teams, "models": sorted(models), "truncated": seen >= MAX_SCAN}


@app.get("/api/entries", dependencies=[Depends(check_token)])
async def list_entries(
    team: str = "*",
    model: str = "*",
    q: str = "",
    sort: str = Query("hits", pattern="^(hits|created_at|last_hit_at)$"),
    limit: int = Query(200, ge=1, le=1000),
):
    """List entries, optionally filtered by team, model and prompt text, sorted descending."""
    pattern = f"{cfg.key_prefix}{team or '*'}:{model or '*'}:*"
    keys: list[str] = []
    async for key in r().scan_iter(match=pattern, count=1000):
        if key != state["cache"].stats_key:
            keys.append(key)
        if len(keys) >= MAX_SCAN:
            break

    fields = ("prompt", "hits", "created_at", "last_hit_at", "usage_tokens")
    pipe = r().pipeline(transaction=False)
    for k in keys:
        pipe.hmget(k, *fields)
        pipe.ttl(k)
    raw = await pipe.execute()

    needle = q.strip().lower()
    rows = []
    for i, k in enumerate(keys):
        vals, ttl = raw[2 * i], raw[2 * i + 1]
        if vals[0] is None:  # expired between SCAN and HMGET
            continue
        if needle and needle not in vals[0].lower():
            continue
        row = parse_key(k)
        row.update(
            prompt=vals[0],
            hits=int(vals[1] or 0),
            created_at=int(vals[2] or 0),
            last_hit_at=int(vals[3] or 0),
            usage_tokens=int(vals[4]) if vals[4] else None,
            ttl=ttl,
        )
        rows.append(row)

    rows.sort(key=lambda x: x[sort], reverse=True)
    return {"total": len(rows), "entries": rows[:limit], "truncated": len(keys) >= MAX_SCAN}


@app.get("/api/entry", dependencies=[Depends(check_token)])
async def get_entry(key: str):
    require_entry_key(key)
    data = await r().hgetall(key)
    if not data:
        raise HTTPException(404, "Entry not found; it may have expired or been evicted")
    data.update(parse_key(key))
    data["ttl"] = await r().ttl(key)
    for f in ("hits", "created_at", "last_hit_at", "usage_tokens"):
        if f in data:
            data[f] = int(data[f])
    return data


@app.delete("/api/entry", dependencies=[Depends(check_token)])
async def delete_entry(key: str):
    require_entry_key(key)
    return {"deleted": bool(await r().delete(key))}


@app.post("/api/clear", dependencies=[Depends(check_token)])
async def clear(team: str | None = None, reset_stats: bool = False):
    if team is not None and (not team or "*" in team or ":" in team):
        raise HTTPException(400, "Invalid team")
    return {"deleted": await state["cache"].clear(team, reset_stats=reset_stats)}


@app.post("/api/stats/reset", dependencies=[Depends(check_token)])
async def reset_stats():
    await r().delete(state["cache"].stats_key)
    return {"reset": True}
