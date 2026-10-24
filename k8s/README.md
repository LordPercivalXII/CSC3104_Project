# Kubernetes (kind) deployment

3 gateway pods behind a Service, plus one Qdrant pod with a volume.

    Browser -> Service (NodePort 30080 -> http://localhost:8080) -> 3-6 gateway pods -> Qdrant

Run from WSL (these are bash scripts), with Docker Desktop running and kind + kubectl installed:

    k8s/up.sh      # create the cluster, build the image, deploy
    k8s/test.sh    # pods, load balancing, shared cache, self-healing, config
    k8s/down.sh    # delete the cluster

Then open http://localhost:8080. The first build is slow; give Docker at least ~6 GB of memory,
since each gateway pod loads PyTorch and the embedding model.

## What is in this folder

| File | Purpose |
|---|---|
| `gateway.yaml` | Deployment (3 pods), Service (NodePort), autoscaler (3-6), disruption budget |
| `qdrant.yaml` | Qdrant StatefulSet with a 2Gi volume, and its Service |
| `namespace.yaml` | The `llm-gateway` namespace |
| `kustomization.yaml` | Ties the files together and builds the ConfigMap from `gateway.config` |
| `kind-config.yaml` | Local cluster: 1 control-plane + 2 workers, port 8080 -> 30080 |
| `up.sh` / `test.sh` / `down.sh` | Create and deploy / check everything / delete the cluster |

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
- **Readiness:** a pod receives traffic only when the app answers `/health` and Qdrant answers `/readyz`.
- **Startup:** the startup probe allows about 3 minutes for the embedding model to load.
- **Graceful stop:** a 5 s `preStop` sleep lets the load balancer drop the pod before it shuts down.
- **Self-healing:** delete a pod and the Deployment starts a replacement (`test.sh` step 5).
- **Autoscaling:** 3 to 6 pods at 70% CPU (needs metrics-server, which `up.sh` installs).
- **Disruption budget:** at least 2 pods stay up during voluntary disruptions such as node drains.

## How it fits the code on main

- The app reads its settings from `smart_ai_gateway/gateway.config` (loaded by `smart_ai_gateway/config.py`).
- Each pod's init container runs `smart_ai_gateway/scripts/setup_qdrant.py` to create the collection.
- The Dockerfile installs CPU-only PyTorch and downloads the embedding model at build time.

## Checking it

    kubectl -n llm-gateway get pods -o wide      # 3 gateway pods + qdrant-0, and which node each is on
    kubectl -n llm-gateway logs -l app=gateway --prefix --tail=20
    k8s/test.sh                                  # step 6 prints the threshold each pod is using

## Notes

- Fair share is per pod: each replica has its own queue, while the semantic cache in Qdrant is shared.
- The mock LLM in `main.py` answers instantly, so the queue order cannot be seen from outside. To watch
  round-robin, add `time.sleep(1)` to `call_ai_model` in `main.py` and send requests from two users at once.
- `/health` only says the app is up. The readiness probe is what also checks Qdrant.
- The semantic cache is shared by everyone: there is no per-user or per-team separation and no expiry.
- Qdrant is a single pod without authentication: fine for a demo, not for production.