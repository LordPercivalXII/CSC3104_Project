# Kubernetes (kind) deployment

3 gateway pods behind a Service, plus one Qdrant pod and one Redis pod (each with a volume), and an
hourly CronJob that deletes aged-out cache entries.

    Browser -> Service (NodePort 30080 -> http://localhost:8080) -> 3-6 gateway pods -> Qdrant (semantic cache)
                                                                                     -> Redis  (fair-share queue, LLM rate limit)

Run from WSL (these are bash scripts), with Docker Desktop running and kind + kubectl installed:

    k8s/up.sh      # create the cluster, build the image, deploy
    k8s/test.sh    # pods, load balancing, shared cache, self-healing, config, cache aging
    k8s/test-scheduler.sh   # shared fair queue, shared LLM rate limit, duplicate questions, Redis restart (mock LLM, ~10 min)
    k8s/down.sh    # delete the cluster

Then open http://localhost:8080. The first build is slow; give Docker at least ~6 GB of memory,
since each gateway pod loads PyTorch, the embedding model and the cache verifier (cross-encoder).

The LLM is Gemini. Put your API key in `.env` at the repo root first (copy `.env.example`), or export
`GEMINI_API_KEY`. `up.sh` stores it in the `gemini-api-key` Secret; it is never committed. Without a
key the gateway pods stop at startup, unless `[llm] provider = mock` in `gateway.config`.
The free tier allows only a few requests per minute and per day (shared by all pods), and `test.sh`
makes about 7 LLM calls per run.

## What is in this folder

| File | Purpose |
|---|---|
| `gateway.yaml` | Deployment (3 pods), Service (NodePort), autoscaler (3-6), disruption budget |
| `qdrant.yaml` | Qdrant StatefulSet with a 2Gi volume, and its Service |
| `redis.yaml` | Redis StatefulSet with a 1Gi volume, and its Service: the scheduler's shared queue and rate limit |
| `cache-sweep.yaml` | Hourly CronJob that deletes expired semantic-cache entries |
| `namespace.yaml` | The `llm-gateway` namespace |
| `kustomization.yaml` | Ties the files together and builds the ConfigMap from `gateway.config` |
| `kind-config.yaml` | Local cluster: 1 control-plane + 2 workers, port 8080 -> 30080 |
| `up.sh` / `test.sh` / `down.sh` | Create and deploy / check everything / delete the cluster |
| `test-scheduler.sh` | Checks the shared fair queue, the shared rate limit, duplicate questions and a Redis restart |

## Scheduler: one fair queue for all pods

Cache misses wait in a fair-share queue (each employee takes turns) before the LLM is called. In
Kubernetes the queue lives in Redis, so all gateway pods share one queue and one LLM rate limit;
with a queue per pod, fairness and the limit would only hold inside each pod. Set in `gateway.yaml`:

| Variable | Value | Meaning |
|---|---|---|
| `SCHEDULER_BACKEND` | `redis` | `memory` = a queue per pod (the default outside Kubernetes) |
| `REDIS_URL` | `redis://redis:6379/0` | The Redis Service |
| `LLM_RATE_LIMIT_PER_MINUTE` | `10` | LLM calls per minute for all pods together; `0` = no limit. Keep it below the Gemini free-tier requests per minute, which are per Google project and shared by every pod |
| `SCHEDULER_WORKERS` | not set (1) | LLM calls each pod makes at the same time |
| `SCHEDULER_COALESCE` | not set (`true`) | Identical questions asked at the same time share one LLM call; `false` turns it off |
| `SCHEDULER_REQUEST_TIMEOUT_SECONDS` | not set (120) | Bounds each HTTP request and queued job |
| `LLM_RATE_LIMIT_MODE` | not set (`sliding`) | `paced` spaces admissions to prevent an initial burst |

Duplicate questions: when several people ask the same question (ignoring case and spacing) while
it is still waiting or being answered, only the first takes a queue turn and an LLM call; the others
get the same answer, and it is cached once. Reworded questions are left to the semantic cache.

Redis runs with `appendonly yes` and `maxmemory-policy volatile-lru`. Pending records, queue state and
processing leases have no TTL, while terminal records/results expire. Recovery uses fenced leases and
retained results; it may repeat an inference whose outcome was lost, so execution is at least once.
An append-only log reduces restart loss but is not a guarantee against Redis data loss.
If Redis is down the gateway processes remain alive, but `/ready` returns 503 and Kubernetes removes
them from Service endpoints until dependencies recover. A direct pod request can still use the cache.
Drain traffic before deploying the revised scheduler; do not mix its state protocol with older workers.

## Settings: the ConfigMap

All app settings live in `smart_ai_gateway/gateway.config`. `up.sh` turns that file into a ConfigMap
(`gateway-config`) and mounts it read-only at `/etc/gateway/gateway.config` in every gateway pod
and in the init container (`GATEWAY_CONFIG` points there). One copy, shared by all 3 pods.
`QDRANT_URL` (set in `gateway.yaml`) still overrides `qdrant_url` in the file.

To change a setting (for example `similarity_threshold`):

```bash
# edit smart_ai_gateway/gateway.config, then
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s | kubectl apply -f -
```

No image rebuild is needed. The ConfigMap name carries a hash of the file, so a change rolls the pods
one at a time automatically. (`kubectl apply -k k8s` alone fails: kustomize does not allow reading a
file from outside `k8s/` unless you pass that flag. `up.sh` already uses it.)

## Availability settings in gateway.yaml

- **Rolling updates:** `maxSurge: 1, maxUnavailable: 0`. A new pod must be ready before an old one stops.
- **Spread:** a topology spread constraint puts the pods on different nodes where possible.
- **Readiness:** `/ready` checks that the Qdrant collection exists and the Redis scheduler answers PING.
  `/health` stays independent for liveness.
- **Startup:** the startup probe allows about 3 minutes for the embedding model to load.
- **Graceful stop:** a 5 s `preStop` sleep lets the load balancer drop the pod before it shuts down.
- **Self-healing:** delete a pod and the Deployment starts a replacement (`test.sh` step 5).
- **Autoscaling:** 3 to 6 pods at 70% CPU (needs metrics-server, which `up.sh` installs).
- **Disruption budget:** at least 2 pods stay up during voluntary disruptions such as node drains.

## How it fits the code on main

- The app reads its settings from `smart_ai_gateway/gateway.config` (loaded by `smart_ai_gateway/config.py`).
- Each pod's init container runs `smart_ai_gateway/scripts/setup_qdrant.py` to create the collection.
- The Dockerfile installs CPU-only PyTorch and downloads the embedding model and the verifier at build time.

## Checking it

    kubectl -n llm-gateway get pods -o wide      # 3 gateway pods + qdrant-0 + redis-0, and which node each is on
    kubectl -n llm-gateway logs -l app=gateway --prefix --tail=20
    k8s/test.sh                                  # step 6 prints the threshold each pod is using

## Notes

- Fair share covers all pods: they take turns from one queue in Redis, like the semantic cache in Qdrant.
- Each pod sends one LLM request at a time, in fair-share order. With `provider = mock` answers are
  instant, so the queue order cannot be seen from outside unless the rate limit holds requests back
  (`test-scheduler.sh` does this); with Gemini, send requests from two users at once and watch the
  `Processing request` log lines.
- A Gemini failure (rate limit, timeout, blocked prompt) returns HTTP 503 and nothing is cached.
- `/health` only says the app is up. The readiness probe is what also checks Qdrant.
- The semantic cache is shared by everyone: there is no per-user or per-team separation.
- Cache entries age out (aging settings in `smart_ai_gateway/gateway.config`). Expired entries are never
  served: the gateway filters them out in Qdrant. The `cache-sweep` CronJob deletes them hourly to free
  storage; run it now with `kubectl -n llm-gateway create job --from=cronjob/cache-sweep cache-sweep-now`.
- Qdrant and Redis are single pods without authentication: fine for a demo, not for production.
