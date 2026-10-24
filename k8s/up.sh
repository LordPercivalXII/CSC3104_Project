#!/usr/bin/env bash
# Create (or update) the local kind cluster: 3 gateway pods + Qdrant.
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
kubectl -n $NS rollout restart deploy/gateway >/dev/null   # pick up a rebuilt image
kubectl -n $NS rollout status sts/qdrant --timeout=180s
kubectl -n $NS rollout status deploy/gateway --timeout=300s
echo "Ready: http://localhost:8080  (try k8s/test.sh)"
