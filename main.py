import asyncio
from contextlib import asynccontextmanager
from scheduler.scheduler_worker import SchedulerWorker

from pathlib import Path
# This annotated thing is a tool in Python's typing module used to add extra, custom metadata to type hints.
from typing import Annotated

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from smart_ai_gateway.services.semantic_cache import (generate_embedding, search_cache, store_cache)

from starlette.concurrency import run_in_threadpool

def call_ai_model(prompt: str) -> str:
    """
    Placeholder function to call the AI model.
    In a real implementation, this would interface with an AI service.
    """
    # For now, return a mock response
    return f"Generated AI response to: {prompt}"

scheduler_worker = SchedulerWorker(inference_fn=call_ai_model)


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
async def chat(request: ChatRequest):
    user_prompt = request.prompt

    # Run synchronous embedding and database calls off the event loop.
    embedding = await run_in_threadpool(
        generate_embedding, user_prompt
    )
    cached_result = await run_in_threadpool(
        search_cache, embedding
    )

    # Cached answers do not need a turn on the inference worker.
    if cached_result is not None:
        return {
            "response": cached_result["response"],
            "source": "semantic_cache",
            "similarity_score": cached_result["similarity_score"],
        }

    # Queue the cache miss and wait without blocking other HTTP requests.
    future = scheduler_worker.submit(
        request.user_id,
        user_prompt,
    )
    result = await asyncio.wrap_future(future)
    answer = result["response"]

    # Store only successfully generated answers.
    await run_in_threadpool(
        store_cache,
        prompt=user_prompt,
        response=answer,
        embedding=embedding,
    )

    return {
        "response": answer,
        "source": "mock_llm",
        "similarity_score": None,
        "request_id": result["request_id"],
    }

# This is used for testing the scheduler without the caching.
@app.post("/scheduler/chat")
async def scheduler_chat(request: SchedulerChatRequest):
    future = scheduler_worker.submit(
        request.user_id,
        request.prompt
    )

    result = await asyncio.wrap_future(future)

    return result