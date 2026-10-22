# Build context is the caching/ folder (set in docker-compose.yml), so the
# paths below are relative to caching/.
FROM python:3.12-slim
WORKDIR /app

# Dependencies are installed directly, so the build no longer needs requirements.txt.
RUN pip install --no-cache-dir "redis>=5.0" "fastapi>=0.110" "uvicorn>=0.29"

COPY gateway ./gateway
COPY dashboard ./dashboard

EXPOSE 8080
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
