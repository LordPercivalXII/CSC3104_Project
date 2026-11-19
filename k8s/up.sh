#!/usr/bin/env bash
# Create (or update) the local kind cluster: 3 gateway pods + Qdrant + Redis.
#
#   bash k8s/up.sh                 build the image and deploy
#   SKIP_BUILD=1 bash k8s/up.sh    deploy an existing smart-gateway:dev image (e.g. from docker load)
#   MOCK_LLM=1 bash k8s/up.sh      use the mock LLM even if a Gemini key is available
# Without a Gemini key (environment or .env) the gateway runs with the mock LLM.
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root
CLUSTER=llm-gateway
NS=llm-gateway

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config k8s/kind-config.yaml

# First build is slow (CPU PyTorch + embedding model); later builds are cached.
if [ "${SKIP_BUILD:-0}" = 1 ]; then
  docker image inspect smart-gateway:dev >/dev/null 2>&1 \
    || { echo "SKIP_BUILD=1 but there is no smart-gateway:dev image (docker load it first)"; exit 1; }
else
  docker build -t smart-gateway:dev .
fi
kind load docker-image smart-gateway:dev --name "$CLUSTER"

# metrics-server so the autoscaler can read CPU on kind (best effort)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null || true
kubectl -n kube-system patch deploy metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null 2>&1 || true

# The ConfigMap is built from smart_ai_gateway/gateway.config, which is outside k8s/
kubectl kustomize --load-restrictor LoadRestrictionsNone k8s | kubectl apply -f -

# Gemini API key for the LLM, from the environment or from .env (git-ignored), stored as a Secret
if [ -z "${GEMINI_API_KEY:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
GEMINI_API_KEY=${GEMINI_API_KEY:-}
GEMINI_API_KEY=${GEMINI_API_KEY%$'\r'}   # .env saved with Windows line endings
if [ -n "$GEMINI_API_KEY" ]; then
  kubectl -n $NS create secret generic gemini-api-key --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY"     --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
if [ -z "$GEMINI_API_KEY" ] || [ "${MOCK_LLM:-0}" = 1 ]; then
  # No key (or MOCK_LLM=1): instant placeholder answers; cache, scheduler and Kubernetes run normally
  echo "LLM: mock (no Gemini key, or MOCK_LLM=1)"
  kubectl -n $NS set env deploy/gateway GW_LLM_PROVIDER=mock >/dev/null
else
  echo "LLM: Gemini (key from the environment or .env)"
  kubectl -n $NS set env deploy/gateway GW_LLM_PROVIDER- >/dev/null   # drop an earlier mock override
fi

kubectl -n $NS rollout restart deploy/gateway >/dev/null   # pick up a rebuilt image and the key
kubectl -n $NS rollout status sts/qdrant --timeout=180s
kubectl -n $NS rollout status sts/redis --timeout=180s
kubectl -n $NS rollout status deploy/gateway --timeout=300s
echo "Ready: http://localhost:8080  (try k8s/test.sh)"
