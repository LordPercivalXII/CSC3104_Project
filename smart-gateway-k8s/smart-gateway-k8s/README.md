# Smart Gateway on Kubernetes (kind)

Your `llm-gateway-infra` cluster, with the merged Smart Gateway (`app/`) in place of the
skeleton gateway.

    Client -> Service (NodePort 30080 -> localhost:8080) -> 3-6 gateway pods
                                                              |-> Redis  (exact cache)
                                                              |-> Qdrant (semantic cache)
                                                              '-> mock LLM (inside the gateway)

## Run (WSL, Docker Desktop, kind, kubectl)
    ./up.sh      # create cluster, build image, deploy (first build is slow: PyTorch + model)
    ./test.sh    # load balancing, exact + semantic cache, fair share, self-healing
    ./down.sh    # delete the cluster

Then open http://localhost:8080 (chat page) or http://localhost:8080/docs.
Give kind at least ~6 GB of Docker memory: each gateway pod loads PyTorch and the embedding model.

## What changed from llm-gateway-infra
| Was | Now |
|---|---|
| `gateway/app.py` pass-through proxy (`/v1/chat/completions`, `/healthz`) | `app/` merged gateway: `POST /chat`, `GET /health`, `GET /cache/stats` |
| `mock-llm` Deployment | removed; the gateway has its own mock LLM (1 s sleep) |
| Redis Stack | plain Redis 7.4 with the LRU settings from docker-compose |
| (none) | `k8s/qdrant.yaml` StatefulSet + 2 Gi volume |
| a few env vars | all gateway settings in `k8s/config.yaml` (CACHE_MODE, FAIR_SHARE_ENABLED, thresholds) |
| probes on `/healthz` | `startupProbe` (up to 3 min) + probes on `/health`; bigger memory requests |
| `x-cache` header | `X-Gateway-Replica` header on `/chat` (the pod name); cache source is in the JSON body |

Unchanged: kind-config.yaml, namespace, PDB, HPA (now 3-6), Service, down.sh.

## Change settings
Edit `k8s/config.yaml`, then:

    kubectl apply -k k8s && kubectl -n llm-gateway rollout restart deploy/gateway

## Limits to know about
- Fair share is per pod. Each replica has its own queue, so across 3 pods one user can run 3 jobs
  at once. `test.sh` step 5 port-forwards to a single pod to demonstrate it. A shared queue
  (e.g. in Redis) is the fix if it needs to hold cluster-wide.
- The semantic cache has no team or model scoping (see `app/README.md`).
- HPA scales on CPU; the mock LLM only sleeps, so load tests will not trigger scaling.
- Redis and Qdrant are single pods without auth; fine for a demo.
- Not yet run on a real cluster: the manifests are YAML-checked and the app is tested, but the
  image build and `./up.sh` need Docker, which was unavailable where this was prepared.
