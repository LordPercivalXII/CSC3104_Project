import asyncio
from contextlib import asynccontextmanager
from scheduler.factory import create_scheduler_worker
from scheduler.redis_backend import RemoteInferenceError

from pathlib import Path
# This annotated thing is a tool in Python's typing module used to add extra, custom metadata to type hints.
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from smart_ai_gateway.services.semantic_cache import (cache_skip_reason, generate_embedding, record_hit, search_cache, store_cache)
from smart_ai_gateway.services.llm import LLMUnavailable, generate_answer, source_name

from starlette.concurrency import run_in_threadpool

# The LLM ([llm] in gateway.config) answers cache misses, one request at a time in fair-share order
# SCHEDULER_BACKEND=redis shares one queue between all gateway pods (see scheduler/factory.py)
scheduler_worker = create_scheduler_worker(inference_fn=generate_answer)


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

# Resolve paths relative to this Python file
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Serve CSS, JavaScript, and other static files
app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)


# Expected JSON request body
class ChatRequest(BaseModel):
    user_id : Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
        ),
    ]
    prompt: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=4000,
        ),
    ]

class SchedulerChatRequest(BaseModel):
    user_id: str
    prompt: str

# END POINTS HERE, can probably move them to another folder later.
# Serve the frontend webpage
@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


# Health-check endpoint
@app.get("/health")
def health():
    return {"status": "healthy"}


# Receive a user's prompt
@app.post("/chat")
async def chat(request: ChatRequest, background_tasks: BackgroundTasks):
    user_prompt = request.prompt

    # Time-sensitive and over-long prompts bypass the cache: not searched, not stored.
    skip_reason = await run_in_threadpool(
        cache_skip_reason, user_prompt
    )
    if skip_reason is not None:
        print(f"Cache skipped: {skip_reason}")

    # Run synchronous embedding and database calls off the event loop.
    embedding, cached_result = None, None
    if skip_reason is None:
        embedding = await run_in_threadpool(
            generate_embedding, user_prompt
        )
        cached_result = await run_in_threadpool(
            search_cache, user_prompt, embedding
        )

    # Cached answers do not need a turn on the inference worker.
    if cached_result is not None:
        # Refresh the entry's last-hit time after the response is sent (keeps it alive with sliding_ttl)
        background_tasks.add_task(
            record_hit,
            cached_result["point_id"],
            cached_result["hit_count"],
        )
        return {
            "response": cached_result["response"],
            "source": "semantic_cache",
            "similarity_score": cached_result["similarity_score"],
            "effective_score": cached_result["effective_score"],
            "match_type": cached_result["match_type"],
            "verifier_score": cached_result["verifier_score"],
            "age_seconds": cached_result["age_seconds"],
        }

    # Queue the cache miss and wait without blocking other HTTP requests.
    # Cacheable questions asked at the same time share one LLM call.
    future = scheduler_worker.submit(
        request.user_id,
        user_prompt,
        dedup=skip_reason is None,
    )
    result = await wait_for_llm(future)
    answer = result["response"]
    coalesced = result.get("coalesced", False)

    # Store only successfully generated answers, once per LLM call.
    if skip_reason is None and not coalesced:
        await run_in_threadpool(
            store_cache,
            prompt=user_prompt,
            response=answer,
            embedding=embedding,
        )

    return {
        "response": answer,
        "source": source_name(),
        "similarity_score": None,
        "cache_skipped": skip_reason,
        "coalesced": coalesced,
        "request_id": result["request_id"],
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