import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from scheduler.factory import create_scheduler_worker
from scheduler.redis_backend import RemoteInferenceError

from pathlib import Path
# This annotated thing is a tool in Python's typing module used to add extra, custom metadata to type hints.
from typing import Annotated, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from smart_ai_gateway.config import get_settings, resolve_path
from smart_ai_gateway.services import semantic_cache
from smart_ai_gateway.services.semantic_cache import (
    append_jsonl, cache_skip_reason, generate_embedding, record_hit, report_wrong_match,
    search_cache_detailed, store_cache, store_paraphrase,
)
from smart_ai_gateway.services.llm import LLMUnavailable, generate_answer, source_name

from starlette.concurrency import run_in_threadpool
from demo.middleware import GatewayMetricsMiddleware
from demo.telemetry import CONTENT_TYPE_LATEST, metrics

# [gateway] in gateway.config: request limits and the decision log
_gateway_cfg = get_settings().gateway

# The LLM ([llm] in gateway.config) answers cache misses, one request at a time in fair-share order
# SCHEDULER_BACKEND=redis shares one queue between all gateway pods (see scheduler/factory.py)
def _observed_generate_answer(prompt: str):
    """Count inference executions on the processing pod, including failed calls.

    These are scheduler-level calls; Gemini's internal HTTP retries are not counted.
    """
    provider = get_settings().llm.provider
    started = metrics.inference_started(provider)
    try:
        answer = generate_answer(prompt)
    except Exception:
        metrics.inference_finished(provider, started, error=True)
        raise
    metrics.inference_finished(provider, started, response=answer)
    return answer


scheduler_worker = create_scheduler_worker(inference_fn=_observed_generate_answer)
metrics.configure(
    scheduler_worker,
    get_settings().llm.provider,
    get_settings().llm.model,
    os.getenv("SCHEDULER_BACKEND", "memory").lower(),
)


async def wait_for_llm(future):
    """
    Wait for the scheduler to return the LLM's answer, turning LLM failures into a 503.
    """
    try:
        return await asyncio.wrap_future(future)
    # RemoteInferenceError: the LLM call failed on another pod (redis mode)
    except (LLMUnavailable, RemoteInferenceError) as exc:
        print(f"LLM unavailable: {exc}")
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "60"} if exc.rate_limited else None,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_worker.start()
    try:
        yield
    finally:
        scheduler_worker.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(GatewayMetricsMiddleware)

# Resolve paths relative to this Python file
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Serve CSS, JavaScript, and other static files
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)


# Expected JSON request body. The length limits come from [gateway] in gateway.config.
class ChatRequest(BaseModel):
    user_id : Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=_gateway_cfg.user_id_max_length,
        ),
    ]
    prompt: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=_gateway_cfg.prompt_max_length,
        ),
    ]

class SchedulerChatRequest(BaseModel):
    user_id: str
    prompt: str

# "This answer was wrong" (POST /feedback)
class FeedbackRequest(BaseModel):
    # The "point_id" of the cached answer, from the /chat response
    point_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    # The question the user asked (not the cached one)
    prompt: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=_gateway_cfg.prompt_max_length),
    ]
    reason: Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]] = None


# --- background helpers: they run after the response is sent, and never raise ----------

def _store_in_background(prompt: str, answer: str, embedding: list[float]):
    # A Qdrant failure here only means this answer is not cached (REVIEW C8)
    try:
        store_cache(prompt=prompt, response=answer, embedding=embedding)
    except Exception as exc:
        print(f"Could not store the answer in the semantic cache: {exc!r}")


def _store_paraphrase_in_background(prompt: str, embedding: list[float], hit: dict):
    try:
        store_paraphrase(prompt, embedding, hit)
    except Exception as exc:
        print(f"Could not store the new wording in the semantic cache: {exc!r}")


def _log_decision(record: dict):
    """
    Append one line to the decision log (decision_log_path), if it is on.
    Logging must never break a request, so errors are only printed.
    """
    if not _gateway_cfg.decision_log_enabled:
        return
    try:
        append_jsonl(resolve_path(_gateway_cfg.decision_log_path), record)
    except Exception as exc:
        print(f"Could not write the decision log: {exc!r}")


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _new_decision(request: ChatRequest) -> dict:
    """
    The decision-log record for one /chat request, filled in as the request goes along.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": None,
        "user_id": request.user_id,
        "prompt_chars": len(request.prompt),   # the prompt itself is not logged
        "skip_reason": None,
        # skipped | direct_hit | verified_hit | rule_rejected | verifier_rejected |
        # below_threshold | miss | cache_error
        "stage": None,
        "top_similarity": None,
        "effective_score": None,
        "verifier_score": None,
        "rule_reason": None,
        "point_id": None,
        "source": None,
        "coalesced": False,
        "cacheable": None,
        "stored": False,
        "usage": None,
        "error": None,
        "latency_ms": {"total": None, "embed": None, "search": None, "verify": None, "llm": None},
    }


# END POINTS HERE, can probably move them to another folder later.
# Serve the frontend webpage
@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


# Health-check endpoint
@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(content=metrics.render(), headers={"Content-Type": CONTENT_TYPE_LATEST})


# Receive a user's prompt
@app.post("/chat")
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    started = time.perf_counter()
    user_prompt = request.prompt
    decision = _new_decision(request)
    latency = decision["latency_ms"]

    # Time-sensitive, long, pasted and personal prompts bypass the cache: not searched, not stored.
    skip_reason = await run_in_threadpool(
        cache_skip_reason, user_prompt
    )
    if skip_reason is not None:
        print(f"Cache skipped: {skip_reason}")
        decision["skip_reason"], decision["stage"] = skip_reason, "skipped"

    # Run synchronous embedding and database calls off the event loop.
    # cache_ok: the cache was searched without errors, so the answer may be stored in it.
    embedding, cached_result, cache_ok = None, None, False
    if skip_reason is None:
        try:
            phase = time.perf_counter()
            embedding = await run_in_threadpool(
                generate_embedding, user_prompt
            )
            latency["embed"] = _ms_since(phase)
            phase = time.perf_counter()
            cached_result, details = await run_in_threadpool(
                search_cache_detailed, user_prompt, embedding
            )
            cache_ok = True
            latency["search"], latency["verify"] = details["search_ms"], details["verify_ms"]
            decision.update(
                stage=details["stage"],
                top_similarity=details["top_similarity"],
                effective_score=details["best_score"],
                verifier_score=details["verifier_score"],
                rule_reason=details["rule_reason"],
            )
        except Exception as exc:
            # Qdrant down or slow (or the model failed): the cache is optional, so carry on
            # as a cache miss and let the LLM answer (REVIEW C8)
            print(f"Semantic cache unavailable, asking the LLM instead: {exc!r}")
            decision["stage"], decision["error"] = "cache_error", repr(exc)
            # Time lost on the failing phase (embed if it failed there, else search)
            latency["embed" if latency["embed"] is None else "search"] = _ms_since(phase)
            cached_result = None

    # Cached answers do not need a turn on the inference worker.
    if cached_result is not None:
        # Refresh the entry's last-hit time after the response is sent (keeps it alive with sliding_ttl)
        background_tasks.add_task(
            record_hit,
            cached_result["point_id"],
            cached_result["hit_count"],
        )
        # The verifier confirmed a new wording: store it, so next time it is a direct hit
        if cached_result["match_type"] == "verified":
            background_tasks.add_task(
                _store_paraphrase_in_background, user_prompt, embedding, cached_result
            )
        decision.update(point_id=str(cached_result["point_id"]), source="semantic_cache")
        latency["total"] = _ms_since(started)
        metrics.observe_decision(decision)
        background_tasks.add_task(_log_decision, decision)
        return {
            "response": cached_result["response"],
            "source": "semantic_cache",
            "similarity_score": cached_result["similarity_score"],
            "effective_score": cached_result["effective_score"],
            "match_type": cached_result["match_type"],
            "verifier_score": cached_result["verifier_score"],
            "age_seconds": cached_result["age_seconds"],
            # Send this to POST /feedback if the answer was wrong
            "point_id": str(cached_result["point_id"]),
            # No LLM tokens were used
            "usage": None,
        }

    # Queue the cache miss and wait without blocking other HTTP requests.
    # Cacheable questions asked at the same time share one LLM call.
    future = scheduler_worker.submit(
        request.user_id,
        user_prompt,
        dedup=skip_reason is None,
    )
    phase = time.perf_counter()
    try:
        result = await wait_for_llm(future)
    except HTTPException:
        # Background tasks do not run for an error response, so log straight away
        latency["llm"], latency["total"] = _ms_since(phase), _ms_since(started)
        decision.update(source=source_name(), error="llm_unavailable")
        metrics.observe_decision(decision)
        await run_in_threadpool(_log_decision, decision)
        raise
    latency["llm"] = _ms_since(phase)

    answer = result["response"]
    coalesced = result.get("coalesced", False)
    # False for truncated or safety-stopped answers: returned to the user, but never cached (C9)
    cacheable = result.get("cacheable", True)
    usage = result.get("usage")

    # Store only complete answers, once per LLM call, after the response is sent: a slow or
    # failing Qdrant then costs the user nothing (REVIEW C8).
    store = cache_ok and embedding is not None and not coalesced and cacheable
    if store:
        background_tasks.add_task(_store_in_background, user_prompt, answer, embedding)
    elif cache_ok and not cacheable:
        print(f"Answer not cached: incomplete (finish reason {result.get('finish_reason')})")

    decision.update(
        request_id=result.get("request_id"),
        source=source_name(),
        coalesced=coalesced,
        cacheable=cacheable,
        stored=store,
        usage=usage,
    )
    latency["total"] = _ms_since(started)
    metrics.observe_decision(decision)
    background_tasks.add_task(_log_decision, decision)

    return {
        "response": answer,
        "source": source_name(),
        "similarity_score": None,
        "cache_skipped": skip_reason,
        "coalesced": coalesced,
        "request_id": result["request_id"],
        # Tokens the LLM call used (prompt, output, thinking, total), when the LLM reports them
        "usage": usage,
    }


# "This answer was wrong": removes the cached answer and records the question pair
@app.post("/feedback")
async def feedback(request: FeedbackRequest):
    try:
        outcome = await run_in_threadpool(
            report_wrong_match,
            request.point_id,
            request.prompt,
            request.reason,
            resolve_path(semantic_cache._cfg.feedback_path),
        )
    except Exception as exc:
        print(f"Could not process feedback for point {request.point_id}: {exc!r}")
        raise HTTPException(status_code=503, detail="The cache is unavailable; please try again later.")

    if outcome is None:
        raise HTTPException(status_code=404, detail="No cached answer with this point_id (it may already be removed).")

    return {
        "status": "removed",
        "point_id": outcome["point_id"],
        # The answer itself plus any other wordings that pointed at it
        "deleted_points": outcome["deleted_points"],
        "message": "Thanks. This answer will not be served again; the next person who asks gets a fresh answer.",
    }

# This is used for testing the scheduler without the caching.
@app.post("/scheduler/chat")
async def scheduler_chat(request: SchedulerChatRequest):
    future = scheduler_worker.submit(
        request.user_id,
        request.prompt
    )

    result = await wait_for_llm(future)

    return result
