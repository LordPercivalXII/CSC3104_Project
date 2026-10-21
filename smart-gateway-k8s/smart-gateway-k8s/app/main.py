import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints

from gateway.pipeline import Pipeline
from gateway.settings import GatewaySettings
from scheduler.scheduler_worker import SchedulerWorker

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

settings = GatewaySettings()
scheduler_worker = SchedulerWorker()
pipeline = Pipeline(settings, scheduler_worker)
REPLICA = socket.gethostname()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_worker.start()
    try:
        await pipeline.startup()
        yield
    finally:
        await pipeline.shutdown()
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


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


# Expected JSON request body. user_id / team_id are optional so the existing
# web page, which only sends a prompt, keeps working.
class ChatRequest(BaseModel):
    prompt: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    user_id: Optional[Identifier] = None   # who the fair-share queue charges
    team_id: Optional[Identifier] = None   # scopes the exact-match cache


class SchedulerChatRequest(BaseModel):
    user_id: str
    prompt: str


# Serve the frontend webpage
@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


# Health-check endpoint (always cheap, safe for liveness/readiness probes)
@app.get("/health")
def health():
    return {"status": "healthy", **pipeline.status()}


# Receive a user's prompt: caches -> fair-share queue -> LLM
@app.post("/chat")
async def chat(request: ChatRequest, response: Response):
    response.headers["X-Gateway-Replica"] = REPLICA  # which pod answered (pod name on Kubernetes)
    return await pipeline.handle(
        prompt=request.prompt,
        user_id=request.user_id or settings.default_user_id,
        team_id=request.team_id or settings.default_team_id,
        model=settings.model,
    )


# Exact-match cache counters, for experiment runs
@app.get("/cache/stats")
async def cache_stats():
    return await pipeline.cache_stats()


# Straight to the fair-share queue, bypassing the caches (used by tests/test_scheduler_api.py)
@app.post("/scheduler/chat")
async def scheduler_chat(request: SchedulerChatRequest):
    future = scheduler_worker.submit(
        request.user_id,
        request.prompt
    )

    result = await asyncio.wrap_future(future)

    return result
