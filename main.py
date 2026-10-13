import asyncio
from contextlib import asynccontextmanager
from scheduler.scheduler_worker import SchedulerWorker

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints


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
    user_prompt = request.prompt

    # Mock AI response for initial testing
    response = f"You asked: {user_prompt}"

    return {
        "response": response,
        "source": "mock"
    }

@app.post("/scheduler/chat")
async def scheduler_chat(request: SchedulerChatRequest):
    future = scheduler_worker.submit(
        request.user_id,
        request.prompt
    )

    result = await asyncio.wrap_future(future)

    return result