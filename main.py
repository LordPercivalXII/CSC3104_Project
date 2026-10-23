import asyncio
from contextlib import asynccontextmanager
from scheduler.scheduler_worker import SchedulerWorker

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from smart_ai_gateway.services.semantic_cache import (generate_embedding, search_cache, store_cache)

scheduler_worker = SchedulerWorker()


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
    prompt: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]

class SchedulerChatRequest(BaseModel):
    user_id: str
    prompt: str

def call_ai_model(prompt: str) -> str:
    """
    Placeholder function to call the AI model.
    In a real implementation, this would interface with an AI service.
    """
    # For now, return a mock response
    return f"Generated AI response to: {prompt}"

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
def chat(request: ChatRequest):
    user_prompt = request.prompt.strip()

    # Mock AI response for initial testing
    response = f"You asked: {user_prompt}"

    if not user_prompt:
        return {
            "response": "Please enter a prompt.",
            "source": "validation"
        }

    # Generate the prompt embedding
    embedding = generate_embedding(user_prompt)

    # Search the Qdrant semantic cache
    cached_result = search_cache(embedding)

    # Check the cache result, if cache hit
    if cached_result is not None:
        return { 
            "response": cached_result["response"],
            "source": "semantic_cache",
            "similarity_score": cached_result["similarity_score"]
        }

    # Else if cache miss - generate a response for now
    # Need call AI model here to give response
    answer = call_ai_model(user_prompt)

    # Store the prompt and answer
    store_cache(prompt=user_prompt, response=answer, embedding=embedding)

    # Return the newly generated answer
    # This will only be returned if the cache miss
    return {
        "response": answer,
        "source": "mock_llm",
        "similarity_score": None
    }

@app.post("/scheduler/chat")
async def scheduler_chat(request: SchedulerChatRequest):
    future = scheduler_worker.submit(
        request.user_id,
        request.prompt
    )

    result = await asyncio.wrap_future(future)

    return result