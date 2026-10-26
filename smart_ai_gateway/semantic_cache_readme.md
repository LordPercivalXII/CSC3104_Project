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
| Embedding model | `BAAI/bge-small-en-v1.5` |

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
2. Time-sensitive or over-long prompts skip the cache (section 9).
3. The embedding model converts the prompt into a vector.
4. The backend searches Qdrant for the closest cached prompts and decides whether one is the same question (section 9).
5. A cache hit returns a stored answer.
6. A cache miss calls the currently configured LLM or mock LLM and stores an eligible new response.

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

## 8. Cache aging

Cached answers do not live forever. Every point stores numeric timestamps next to its prompt and response:

| Payload field | Meaning |
|---|---|
| `created_at_ts` | When the answer was stored (epoch seconds) |
| `last_hit_at_ts` | When it was last served from the cache |
| `hit_count` | How many times it has been served |

The rules live in `smart_ai_gateway/services/cache_aging.py` and are set in the `[semantic_cache]`
section of `gateway.config`:

| Setting | Effect |
|---|---|
| `ttl_seconds` | Entries older than this are never served. `0` = never expire |
| `sliding_ttl` | `true`: age counts from the last hit, so popular answers stay. `false`: from creation |
| `max_age_seconds` | Hard cap on age since creation, however often an entry is hit. `0` = no cap |
| `decay_half_life_seconds` | Optional gradual aging: a match's score halves every half-life and must still reach `similarity_threshold`. `0` = off |
| `search_candidates` | How many nearest entries are considered (shared with matching, section 9) |
| `sweep_interval_seconds` | How often the docker compose sweeper deletes expired entries |

How it works:

1. **Reading:** `search_cache` asks Qdrant only for entries that are still fresh, so an expired entry is
   a cache miss. The LLM answers again and the fresh answer is cached.
2. **On a hit:** after the response is sent, the gateway updates `last_hit_at_ts` and `hit_count`.
3. **Sweeping:** expired entries are deleted to free storage. Docker compose runs the `cache-sweeper`
   service; Kubernetes runs the `cache-sweep` CronJob. To sweep by hand:

```powershell
python smart_ai_gateway/scripts/sweep_expired_cache.py
```

`setup_qdrant.py` creates payload indexes on the timestamp and model fields, including on existing collections.

**Entries cached before aging existed** have no numeric timestamps or model tag, so they are never
served and the sweeper deletes them. They were embedded with `all-MiniLM-L6-v2`, so they are only
useful while `embedding_model` is set back to that model. In that case, backfill them before the
next sweep (timestamps come from `created_at`; the tag is set to MiniLM):

```powershell
python smart_ai_gateway/scripts/backfill_cache_timestamps.py
```

Tests (in-memory Qdrant, no server needed):

```powershell
cd tests
python test_semantic_cache_aging.py
```

## 9. Matching: deciding that two questions are the same

A single similarity threshold cannot both catch reworded questions and reject questions that
differ in one important word. The cache therefore decides in stages
(`select_match` in `services/semantic_cache.py`):

1. **Skip:** prompts about "today", "latest", "this week" and similar, and prompts longer than the
   embedding model's 512-token limit, bypass the cache entirely (`cache_skip_reason`).
2. **Find:** Qdrant returns the `search_candidates` closest fresh entries (bge-small embeddings).
3. **Rule checks** (`services/cache_checks.py`): a candidate is rejected if the numbers, error codes,
   acronyms or product names differ, if only one question is negated ("can" / "can't"), or if they
   use opposite words (enable / disable, install / uninstall, log in / log out, ...).
4. **Direct hit:** similarity at or above `similarity_threshold` (0.95).
5. **Grey zone:** similarity between `verify_threshold` (0.75) and `similarity_threshold` is checked by
   the verifier, a cross-encoder (`cross-encoder/quora-roberta-base`) that reads both questions
   together. It is a hit if the verifier's probability reaches `verifier_threshold` (0.5).

Cache hits return `match_type` (`direct` or `verified`) and `verifier_score`. Misses print their
reason (best score, which rule fired, or the verifier's probability), which shows which setting is
costing hits.

Entries are tagged with the embedding model that made their vector. After changing
`embedding_model`, old entries are never served (vectors from different models cannot be compared)
and the sweeper deletes them.

### Measuring it

`scripts/cache_matching_pairs.py` holds 58 labelled question pairs: 32 reworded questions that should
hit, and 26 near-identical questions that must not (different product, number, negation or
opposite action). Evaluate the current settings against the original setup:

```powershell
python smart_ai_gateway/scripts/evaluate_cache_matching.py --baseline
```

| Setup | Reworded questions caught | Wrong answers served |
|---|---|---|
| Original: MiniLM, threshold 0.90 | 3 / 32 | 1 / 26 |
| bge-small, threshold 0.95, rule checks only | 4 / 32 | 0 / 26 |
| Current: bge-small + rule checks + verifier | 15 / 32 | 0 / 26 |

The set is small and was used to choose the thresholds, so treat these numbers as a guide. Add
pairs from real traffic (the miss log lines help find them) and re-run after changing any setting.
Known gaps: "2FA" vs "two factor authentication" (rule: different numbers) and "account is locked"
vs "unlock my account" (rule: opposite words) are rejected, which costs a hit, never a wrong answer.

## 10. Important development notes

- Do not commit `.venv/`, `.env`, API keys, or Python cache files to GitHub.
- Docker volumes are local to each developer's machine and are not pushed to GitHub.
- Qdrant must be running before the gateway attempts to search or store cached responses.
- Do not manually modify Qdrant's internal storage files.
- The current setup uses Docker for Qdrant and local Python for FastAPI.
- The final architecture will deploy Qdrant and the gateway as Kubernetes workloads.

