#!/usr/bin/env bash
# Create (or update) the local kind cluster: 3 gateway pods + Qdrant + Redis.
set -euo pipefail
cd "$(dirname "$0")/.."          # repo root
CLUSTER=llm-gateway
NS=llm-gateway

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config k8s/kind-config.yaml

# First build is slow (CPU PyTorch + embedding model); later builds are cached.
docker build -t smart-gateway:dev .
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
else
  echo "WARNING: GEMINI_API_KEY is not set (environment or .env). Gateway pods will not start"
  echo "         unless [llm] provider = mock in smart_ai_gateway/gateway.config."
fi

kubectl -n $NS rollout restart deploy/gateway >/dev/null   # pick up a rebuilt image and the key
kubectl -n $NS rollout status sts/qdrant --timeout=180s
kubectl -n $NS rollout status sts/redis --timeout=180s
kubectl -n $NS rollout status deploy/gateway --timeout=300s
echo "Ready: http://localhost:8080  (try k8s/test.sh)"
