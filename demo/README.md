# Gateway demonstration

Locust creates concurrent HTTP requests. Prometheus scrapes each gateway replica.
Grafana displays the measurements. The existing `eval/` experiments remain the
source for labelled correctness and estimated cost comparisons.

The demonstration configuration and monitoring live here. It runs the existing
application, and Locust reads the same `eval/cost/data/intents.json` as the cost
benchmark. Metrics are wired in `main.py` and `demo/` is included in the gateway
image. Normal startup stays `main:app`.

## Choose one startup path

**The Kubernetes demonstration uses `start-kubernetes.ps1`. The Compose commands
are a separate alternative, not another step after starting Kubernetes.**

| Setup | Startup from the repository root | Chat address | Locust Host field |
| --- | --- | --- | --- |
| Kubernetes (kind) | `.\demo\start-kubernetes.ps1` | http://localhost:8080 | `http://gateway` |
| Docker Compose alternative | `docker compose -f demo/compose.yaml up --build -d` | http://localhost:8001 | `http://proxy` |

For Kubernetes, continue with [First Kubernetes setup on Windows](#first-kubernetes-setup-on-windows)
and its port-forwards. If your chatbot, Prometheus, Grafana and Locust are already
open and working through Kubernetes, startup is complete; do not run Compose.
Both alternatives use local ports 3000, 9090 and 8089, so run only one at a time.
Sections marked Compose apply only to the Compose alternative.

## Alternative: Docker Compose without Kubernetes

From the repository root in PowerShell:

```powershell
docker compose -f demo/compose.yaml up --build -d
docker compose -f demo/compose.yaml ps
```

This is a separate Compose project (`smart-gateway-demo`) with its own network,
Qdrant/Redis volumes, `demo_semantic_cache` collection and scheduler prefix. It
starts **three gateway replicas**, not three public websites. Nginx provides one
entry address and distributes requests to them. First build downloads the models.

The default is **mock inference with a one-second delay**. Embeddings, verification,
Qdrant, Redis, coalescing and HTTP requests still run normally. Mock response time
is a simulation, not Gemini performance. The load generator waits for you to click
Start; bringing up the stack sends no chat requests.

| Page | Address | What to show |
| --- | --- | --- |
| Chat | http://localhost:8001 | The existing user-facing application |
| Grafana | http://localhost:3000/d/gateway-demo | Dashboard: Gateway demo |
| Prometheus | http://localhost:9090/targets | One scrape target per gateway replica |
| Locust | http://localhost:8089 | Start/stop users, latency, throughput and failures |

Grafana login: **admin / demo-local-password**. These interfaces bind to localhost.
Set `DEMO_GRAFANA_PASSWORD` before first startup to choose another password. Once
its data volume exists, Grafana retains its existing account password.

In this Compose alternative, start Locust with **2 users**, **1 user/second**, host
**http://proxy** (the container's internal address). Increase gradually only while
the laptop has sufficient memory and remains responsive. Kubernetes uses
**http://gateway** instead.
Wait about a minute to populate rate/percentile panels. Stop the test in Locust
before switching scenarios. The start control is on the Locust page, not Grafana.

## A five-minute presentation

1. Explain the hypothesis: reuse equivalent answers to avoid inference while
   controlling wrong matches. Show the existing evaluation graphs for this claim.
2. On Prometheus Targets show three healthy gateway targets, then on Grafana show
   **Reachable gateway instances**, **Serving configuration by instance**, and the per-pod
   request series. This establishes what is actually running.
3. In the chat ask `how do I reset my password` twice. If the demo collection was
   empty, the first request invokes the mock model and the next reuses the cache.
   Show the cache outcome and inference-call panels. A previously warmed cache may
   hit immediately; do not reset an existing application's cache to force a miss.
4. Start Locust's default cache workload. It uses the evaluation's questions,
   paraphrases, personal templates and time-sensitive prompts: approximately 92%
   FAQ, 5% personal and 3% time-sensitive inputs. Half the FAQ inputs use a
   paraphrase when available. These are **input percentages**, not promised hit
   rates. Show measured hit ratio, latency and inference calls.
5. Switch to `misses` or `scheduler` below. Show increasing queue depth and response
   latency as misses exceed the workers' service capacity. Explain the Redis
   round-robin scheduler. This live workload illustrates queueing; use the existing
   heavy-versus-light evaluation to discuss fairness quantitatively.

For a rule-check example, ask `How do I enable two factor authentication?` and then
`How do I disable two factor authentication?`. If the first answer is cached and
retrieved as a candidate for the second, the opposite-word rule rejects it. Explain
that this is a cache-selection safeguard. Mock answers do not establish factual
correctness, and whether this exact pair is retrieved depends on its vector score.

One-second mock calls and three workers imply only a few completed misses per
second. A warm cache can serve additional requests without inference. The demo
does not set a target speedup: report what your machine actually measures.

## Shared evaluation prompts

The default `cache` scenario reads the canonical dataset at
`eval/cost/data/intents.json`; there is no separate copy to keep in sync. Compose
bind-mounts it read-only, and Kubernetes generates a separate ConfigMap from the
same file. The dataset supplies FAQ questions, all their alternative wordings,
personal templates and time-sensitive questions.

Default sampling uses 5% personal, 3% time-sensitive and 92% FAQ inputs. Within FAQ
inputs, half use a paraphrase when available. FAQ popularity uses Zipf exponent
1.0. These match the cost evaluation's default category/phrasing assumptions, but
Locust does not simulate companies, working days or the evaluation's virtual
clock. A seeded live request stream also changes with concurrency and user count.

The loader accepts `DEMO_DATA_SEED` (default 1), `DEMO_PARAPHRASE_SHARE` (0.5),
`DEMO_PERSONAL_SHARE` (0.05), `DEMO_TIME_SHARE` (0.03) and `DEMO_ZIPF_S` (1.0).
`DEMO_INTENTS_PATH` selects an explicit dataset path; the supplied deployments
set it to `/mnt/data/intents.json`. Compose forwards these settings from the shell.
For Kubernetes, use `kubectl set env deployment/demo-locust` to override sampling.

Using the same prompts does **not** make Locust a correctness evaluator. Its
success/failure checks still validate HTTP status and response structure, not
whether the returned cached answer has the expected intent. Grafana displays live
traffic and cache decisions; the labelled cost benchmark separately scores cache
selection and generates cost estimates and graphs. The normal mock demo also
does not replay the evaluation's recorded Gemini answers.

To load the updated Locust code/data into an existing Kubernetes demo, stop the
Locust test and run from the repository root:

```powershell
kubectl kustomize --load-restrictor LoadRestrictionsNone demo/k8s | kubectl --context kind-llm-gateway apply -f -
kubectl --context kind-llm-gateway -n llm-gateway set env deployment/demo-locust DEMO_SCENARIO=cache
kubectl --context kind-llm-gateway -n llm-gateway rollout status deployment/demo-locust --timeout=300s
```

This applies the monitoring/Locust manifests without changing the gateway's
provider, replicas or cache. The generated ConfigMap hashes trigger a Locust pod
rollout when its code or dataset changes. Restart its port-forward afterward:

```powershell
kubectl --context kind-llm-gateway -n llm-gateway port-forward service/demo-locust 8089:8089
```

For an existing Compose demo, restart only Locust:

```powershell
$env:DEMO_SCENARIO = "cache"
docker compose -f demo/compose.yaml up -d --force-recreate locust
```

## Change the workload (Compose)

The same Locust file supports three scenarios. Stop the current test first:

```powershell
$env:DEMO_SCENARIO = "misses"
docker compose -f demo/compose.yaml up -d --force-recreate locust
```

| Scenario | Endpoint | Purpose |
| --- | --- | --- |
| `cache` (default) | `/chat` | Evaluation FAQs/paraphrases, personal templates and time-sensitive prompts |
| `misses` | `/chat` | Changing printer/device codes; rule checks should prevent reuse |
| `scheduler` | `/scheduler/chat` | Bypass semantic caching and coalescing to show queueing |

In `misses` and `scheduler`, fresh numeric questions do not repeat for 81 million
requests within one Locust process, but another run can overlap the cache. All virtual users keep their own
user IDs and wait 1-3 seconds between requests. Each user has at most one request
outstanding, so this is not the heavy-user burst experiment in `eval/`.

To make the shared rate limiter visible with mock inference:

```powershell
$env:DEMO_LLM_RATE_LIMIT_PER_MINUTE = "30"
docker compose -f demo/compose.yaml up -d gateway
```

This is a shared limit, not 30 per pod. Keep runs short and stop new traffic before
the queue becomes too long; queued requests can outlive Locust's Stop button.
The default is no rate limit. To return to it set this variable to `"0"` and
recreate the gateway again.

## Optional real Gemini demonstration (Compose)

For a small, manual cache miss/hit demonstration, stop Locust and explicitly opt in:

```powershell
$env:DEMO_LLM_PROVIDER = "gemini"
$env:DEMO_LLM_RATE_LIMIT_PER_MINUTE = "5"
docker compose --env-file .env -f demo/compose.yaml up -d gateway
```

The root `.env` supplies `GEMINI_API_KEY`. Use a limit below your actual project
quota. The configured model is in `smart_ai_gateway/gateway.config`; the checked-in
value is `gemini-3.5-flash-lite`, not 3.8. `gateway_info` shows configured values.
The mock delay applies only in mock mode. Gemini answers use different cache tags
from mock answers, so a mock answer is not returned as a Gemini cache hit.

Changing providers does not prove model quality or change Google's project quota.
The inference counter measures scheduler inference executions, including failures,
not individual SDK retry attempts or invoices. Return to mock by setting
`DEMO_LLM_PROVIDER=mock`, `DEMO_LLM_RATE_LIMIT_PER_MINUTE=0` and recreating gateway.

## Add the dashboard to the existing Kubernetes deployment

### First Kubernetes setup on Windows

Docker Compose does not create Kubernetes pods. This repository uses **kind**:
one control-plane container and two worker-node containers inside Docker Desktop.
Each gateway pod serves the same application through one Kubernetes Service.
Kind is independent of Docker Desktop's optional built-in Kubernetes cluster.
Your standalone `smart-qdrant` container remains separate; the cluster creates its
own Qdrant and Redis with their own persistent volumes.

Prerequisites: Docker Desktop running Linux containers, plus `kind` and `kubectl`
on PATH. Check with `kind version` and `kubectl version --client`. Official install
guides: https://kind.sigs.k8s.io/docs/user/quick-start/ and
https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/.

For a new or existing **llm-gateway kind cluster**, start the complete mock demo
from the repository root with one PowerShell command:

```powershell
.\demo\start-kubernetes.ps1
kubectl config use-context kind-llm-gateway
```

It creates the kind cluster if missing, builds/loads the gateway image, installs
metrics-server for the existing CPU HPA, applies the gateway mock overlay and adds
Prometheus, Grafana and Locust. All kubectl mutations explicitly target
`kind-llm-gateway`. The default mock overlay works without an API key, switches the
gateway to a separate demo collection/queue and suspends the regular cache-sweep
CronJob. **It changes an existing llm-gateway deployment to demo mode.** Stop any
existing traffic first. `-SkipBuild` reuses an image you have already rebuilt.

The context command selects the cluster for the following port-forward/watch
examples. The startup script itself explicitly targets that context throughout.

The startup script always selects **mock inference**. Re-running it after switching to Gemini
switches the gateway back to mock. The monitoring dashboard works with either provider, but it
does not read the CSVs or monetary estimates under `eval/cost/`; those are a separate experiment.

The original application-only startup is `bash k8s/up.sh` (Git Bash/WSL), from the
project root. It uses the configured Gemini provider/key, and does not add these
monitoring services. On Windows, the PowerShell demo command avoids requiring Bash.

Continue with the port-forwards below. For a script-created mock deployment,
restoring the regular application requires reapplying the base **and explicitly
removing** demo command/env overrides and the CronJob suspension. When you
intentionally want to restore the base Gemini deployment, stop the demo load,
run `bash k8s/up.sh` with your `.env` key to restore the base config and Secret,
then apply the explicit restoration patches:

```powershell
kubectl --context kind-llm-gateway -n llm-gateway patch deployment gateway --type strategic --patch-file demo/k8s/restore-gateway-patch.yaml
kubectl --context kind-llm-gateway -n llm-gateway patch cronjob cache-sweep --type merge --patch-file demo/k8s/restore-sweep-patch.yaml
kubectl --context kind-llm-gateway -n llm-gateway rollout status deployment/gateway --timeout=300s
```

These patches restore the base settings rather than any custom overrides you
had before the demo. This is different from undoing one optional deployment patch.

### Existing instrumented cluster

Select `kind-llm-gateway` with `kubectl config use-context kind-llm-gateway` before
using the following commands against this repository's kind cluster.

The existing gateway image must first be rebuilt with `/metrics`. With the
repository's existing kind cluster (this is not Docker Desktop's separate cluster):

```powershell
docker build -t smart-gateway:dev .
kind load docker-image smart-gateway:dev --name llm-gateway
kubectl -n llm-gateway rollout restart deployment/gateway
kubectl -n llm-gateway rollout status deployment/gateway --timeout=300s
kubectl kustomize --load-restrictor LoadRestrictionsNone demo/k8s | kubectl apply -f -
kubectl -n llm-gateway rollout status deployment/demo-grafana
kubectl -n llm-gateway rollout status deployment/demo-prometheus
kubectl -n llm-gateway rollout status deployment/demo-locust
```

The monitoring manifests add only demo services and discovery permissions in
`llm-gateway`; they do not change gateway replicas, HPA, or LLM configuration.
The gateway continues using its current provider. **Use the optional mock patch
before starting Locust if this deployment currently uses Gemini:**

```powershell
kubectl -n llm-gateway patch deployment gateway --type strategic --patch-file demo/k8s/mock-patch.yaml
kubectl -n llm-gateway rollout status deployment/gateway --timeout=300s
```

The patch runs delayed mock inference, disables file logs, and uses a separate
collection/queue prefix. It changes the deployed gateway temporarily. Apply it
only when existing requests have finished. To undo that patch before any other
gateway rollout, stop Locust and run:

```powershell
kubectl -n llm-gateway rollout undo deployment/gateway
kubectl -n llm-gateway rollout status deployment/gateway --timeout=300s
```

`rollout undo` restores the preceding revision, so do not use it blindly after
other gateway deployments. Reapplying the base manifest alone is not a reliable
way to remove fields introduced by an imperative patch.

Use one terminal for each port-forward (stop them with Ctrl+C):

```powershell
kubectl -n llm-gateway port-forward service/demo-grafana 3000:3000
kubectl -n llm-gateway port-forward service/demo-prometheus 9090:9090
kubectl -n llm-gateway port-forward service/demo-locust 8089:8089
```

Locust's Kubernetes host is **http://gateway**. The existing chat remains at the
cluster's normal address (kind setup: http://localhost:8080). To change its workload:

```powershell
kubectl -n llm-gateway set env deployment/demo-locust DEMO_SCENARIO=scheduler
kubectl -n llm-gateway rollout status deployment/demo-locust
```

Prometheus discovers each ready gateway pod directly, rather than scraping a
load-balanced Service as if it were one process. Kubernetes demo monitoring data
uses `emptyDir` and disappears when its monitoring pod is replaced. Compose's
named monitoring volumes persist across `down`.

## Read the dashboard correctly

- Cache-hit ratio is direct plus verified hits divided by completed HTTP `/chat`
  requests, including errors/invalid requests in the denominator. `/scheduler/chat`
  deliberately does not contribute. A short scrape/response boundary can
  briefly misalign the counters.
- HTTP latency finishes at the final response body, before cache/log background
  work. The `scheduler_roundtrip` phase includes queue/rate-limit waiting, inference
  and result delivery. The separate inference histogram measures execution of the
  model function only, including SDK retries inside that function.
- Tokens count once on the pod executing inference, not on every coalesced waiter.
  Mock token usage is an estimate. No billing or monetary savings are claimed here.
- Shared Redis queue depth uses **max across replicas**, never a sum of the same
  shared queue. Inference-in-progress is local per pod and can be summed.
- Healthy target count is the number of scraped gateway processes. Compose's
  three replicas are fixed. A count alone does not prove autoscaling. For Kubernetes
  use `kubectl -n llm-gateway get hpa gateway -w` in one terminal and
  `kubectl -n llm-gateway get pods -l app=gateway -w` in another.
- This small stack does not install KEDA, kube-state-metrics or node/cAdvisor
  exporters. It does not claim to graph cluster CPU/RAM or establish scaling limits.
- Prompts, user IDs and request IDs are not metric labels. `/metrics`, health
  checks and static assets do not inflate traffic graphs.
- One Uvicorn process per container is expected. Separate OS processes own separate
  registries; do not use `--workers N` and scrape them through one address.

For the evaluation walkthrough and known limitations see [EVALUATION.md](EVALUATION.md).

## Stop and verify

Stop the Locust test and allow in-flight requests to finish first.

For **Compose**:

```powershell
docker compose -f demo/compose.yaml down
```

This leaves Compose demo data volumes in place.

For **Kubernetes**, press Ctrl+C in any running startup or port-forward terminals.
To temporarily stop the complete kind cluster while retaining its node containers
and stored data:

```powershell
docker stop llm-gateway-worker llm-gateway-worker2 llm-gateway-control-plane
```

`docker compose down` does not stop the Kubernetes cluster. Kubernetes monitoring can be removed with
the same rendered kustomization passed to `kubectl delete -f -`; that removes only
the monitoring resources, not the optional gateway mock patch or the base app.

Offline checks (activate the existing virtual environment and install requirements):

```powershell
python -m pip install -r requirements.txt
python -B -m unittest discover -s demo/tests -v
docker compose -f demo/compose.yaml config --quiet
kubectl kustomize --load-restrictor LoadRestrictionsNone demo/k8s
```

The tests fake embeddings, storage and inference before importing the application.
They do not contact Gemini, Redis or a Qdrant server. They check HTTP failures,
cache miss/hit accounting, coalesced token accounting and workload behaviour.
