# One image for the whole gateway: web app, semantic-cache client and fair-share worker.
# Build context = the repo root (same layout as main).

# Use Python 3.11 with a lightweight Linux environment
FROM python:3.11-slim

# Python configuration
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache

# Set the working directory inside the container
WORKDIR /app

# CPU-only PyTorch first. The default Linux wheel pulls ~2 GB of CUDA libraries that a
# CPU-only embedding model never uses; with this installed, requirements.txt reuses it.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

# Copy the Python dependencies list and install it
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model and the cache verifier (cross-encoder) into the image so every
# container starts without downloading them.
# The names must match embedding_model and verifier_model in smart_ai_gateway/gateway.config.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer('BAAI/bge-small-en-v1.5'); CrossEncoder('cross-encoder/quora-roberta-base')"

# Copy the application files
COPY main.py .
COPY static/ ./static/
COPY scheduler/ ./scheduler/
COPY smart_ai_gateway/ ./smart_ai_gateway/

# Do not run as root
RUN useradd --create-home --uid 1000 app
USER app

# Document the application's listening port
EXPOSE 8000

# Start the FastAPI application
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
