#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
CLUSTER=llm-gateway

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config kind-config.yaml

# First build is slow (CPU PyTorch + embedding model baked in); later builds are cached.
docker build -t smart-gateway:dev app
kind load docker-image smart-gateway:dev --name "$CLUSTER"

# metrics-server so the HPA can read CPU on kind (best effort)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null || true
kubectl -n kube-system patch deploy metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null 2>&1 || true

kubectl apply -k k8s
kubectl -n llm-gateway rollout restart deploy/gateway >/dev/null  # pick up a rebuilt image
kubectl -n llm-gateway rollout status sts/redis --timeout=180s
kubectl -n llm-gateway rollout status sts/qdrant --timeout=180s
kubectl -n llm-gateway rollout status deploy/gateway --timeout=300s
echo "Ready: http://localhost:8080  (try ./test.sh)"
