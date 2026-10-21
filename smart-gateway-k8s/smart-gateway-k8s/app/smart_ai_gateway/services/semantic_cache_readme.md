## 1. Create persistent storage for Qdrant

Qdrant requires storage for its vector collections, embeddings, and cached responses.

Create a Docker named volume:

```powershell
docker volume create smart_qdrant_data
```

Verify that the volume exists:

```powershell
docker volume ls
```

The volume will be managed by Docker and will persist independently of the Qdrant container.

**Note:** This is a Docker named volume, not a Kubernetes PersistentVolumeClaim (PVC). A PVC will be configured when we deploy Qdrant to Kubernetes.

## 2. Download and run the Qdrant server

Pull the official Qdrant image:

```powershell
docker pull qdrant/qdrant:latest
```

Start the Qdrant container:

```powershell
docker run -d `
  --name smart-qdrant `
  -p 127.0.0.1:6333:6333 `
  -v smart_qdrant_data:/qdrant/storage `
  qdrant/qdrant:latest
```

Explanation of the Docker arguments:

| Argument | Purpose |
|---|---|
| `-d` | Runs the container in the background |
| `--name smart-qdrant` | Names the container |
| `-p 127.0.0.1:6333:6333` | Exposes Qdrant's HTTP API on the local computer |
| `-v smart_qdrant_data:/qdrant/storage` | Mounts persistent storage into the container |
| `qdrant/qdrant:latest` | Specifies the Docker image |

The volume is mounted at `/qdrant/storage`, where Qdrant stores its database files.

**Important:** This container is for local development only and does not have authentication configured. Do not expose it publicly.

## 3. Verify that Qdrant is running

List running containers:

```powershell
docker ps
```

Check the Qdrant server logs:

```powershell
docker logs smart-qdrant
```

Test the HTTP API:

```powershell
Invoke-RestMethod http://localhost:6333
```

Open the Qdrant dashboard in a browser:

http://localhost:6333/dashboard

The dashboard allows you to inspect Qdrant collections and query their stored points.

## 4. Set up the Python environment

Create a virtual environment if one does not already exist:

```powershell
py -3.11 -m venv smart_ai_gateway/.venv
```

Activate it:

```powershell
.\smart_ai_gateway\.venv\Scripts\Activate.ps1
```

Install the project dependencies:

```powershell
python -m pip install -r requirements.txt
```

The Python application uses:

- `fastapi` and `uvicorn` for the web application.
- `qdrant-client` to communicate with Qdrant.
- `sentence-transformers` to generate text embeddings.

The embedding model will be downloaded on its first use.

## 5. Initialize the Qdrant collection

Run the setup script:

```powershell
python smart_ai_gateway/scripts/setup_qdrant.py
```

This creates the `semantic_cache` collection if it does not already exist.

Collection configuration:

| Property | Value |
|---|---|
| Collection name | `semantic_cache` |
| Vector dimensions | 384 |
| Similarity metric | Cosine |
| Embedding model | `all-MiniLM-L6-v2` |

Open the Qdrant dashboard and verify that the `semantic_cache` collection exists.

Each teammate starts with their own local volume and an initially empty collection. Cached data from another developer's machine is not included in the Git repository.

## 6. Run the FastAPI application locally

With the Python virtual environment activated, run:

```powershell
python -m uvicorn main:app --reload
```

Open the application:

http://localhost:8000

The employee chat interface should appear.

When a user submits a prompt:

1. FastAPI receives the POST request at `/chat`.
2. The embedding model converts the prompt into a vector.
3. The backend searches Qdrant for a sufficiently similar cached prompt.
4. A cache hit returns a stored answer.
5. A cache miss calls the currently configured LLM or mock LLM and stores an eligible new response.

The application uses `http://localhost:6333` to connect to Qdrant when running directly on Windows.

## 7. Managing the Qdrant container

After the container has been created, it does not need to be recreated every time.

**Stop Qdrant:**

```powershell
docker stop smart-qdrant
```

**Restart the existing container:**

```powershell
docker start smart-qdrant
```

**Check its status:**

```powershell
docker ps -a
```

**Inspect its persistent volume:**

```powershell
docker volume inspect smart_qdrant_data
```

**Remove the Qdrant container without deleting its named volume:**

```powershell
docker stop smart-qdrant
docker rm smart-qdrant
```

The named volume remains available and can be mounted into a new Qdrant container.

### Deleting all stored Qdrant data

If the development database needs to be completely reset, first stop and remove the Qdrant container:

```powershell
docker stop smart-qdrant
docker rm smart-qdrant
```

Then remove its volume:

```powershell
docker volume rm smart_qdrant_data
```

**Warning:** Deleting the volume permanently removes its stored Qdrant data. Recreate the volume and container using Steps 3–4, then rerun the collection setup script.

## 8. Important development notes

- Do not commit `.venv/`, `.env`, API keys, or Python cache files to GitHub.
- Docker volumes are local to each developer's machine and are not pushed to GitHub.
- Qdrant must be running before the gateway attempts to search or store cached responses.
- Do not manually modify Qdrant's internal storage files.
- The current setup uses Docker for Qdrant and local Python for FastAPI.
- The final architecture will deploy Qdrant and the gateway as Kubernetes workloads.

