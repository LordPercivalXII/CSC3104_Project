# Single image for the whole gateway: API + caches + fair-share scheduler.
# (The per-branch Dockerfiles only copied main.py and static/, so the
# scheduler/, caching/ and smart_ai_gateway/ packages were missing at runtime.)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache

WORKDIR /app

# CPU-only PyTorch first: the default Linux wheel drags in ~2 GB of CUDA libraries
# that a CPU-only embedding model never uses.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt requirements-embedding.txt ./
RUN pip install -r requirements.txt -r requirements-embedding.txt

# Bake the embedding model into the image so replicas start without downloading it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY main.py .
COPY gateway ./gateway/
COPY caching ./caching/
COPY smart_ai_gateway ./smart_ai_gateway/
COPY scheduler ./scheduler/
COPY static ./static/

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
