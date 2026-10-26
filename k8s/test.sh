#!/usr/bin/env bash
# Checks: 3 gateway pods + Qdrant, load balancing, shared semantic cache, self-healing, cache aging.
URL=${URL:-http://localhost:8080}
NS=llm-gateway

# ask <user> <question>  ->  "source=<semantic_cache|gemini|mock_llm> score=<similarity>"
ask() {
  local out
  out=$(curl -s -X POST "$URL/chat" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$1\",\"prompt\":\"$2\"}")
  echo "source=$(echo "$out" | grep -o '"source":"[^"]*"' | cut -d'"' -f4) score=$(echo "$out" | grep -o '"similarity_score":[^,}]*' | cut -d: -f2)"
}

echo "== 0. Pods: expect 3 gateway pods and qdrant-0, all Running"
kubectl -n $NS get pods -o wide

# Start from an empty cache so results don't depend on earlier runs.
kubectl -n $NS exec deploy/gateway -- python -c "
import os
from qdrant_client import QdrantClient, models
c = QdrantClient(url=os.environ['QDRANT_URL'])
c.delete('semantic_cache', points_selector=models.FilterSelector(filter=models.Filter(must=[])), wait=True)
print('cache cleared')"
echo

echo "== 1. Six different questions (all answered by the LLM, none from semantic_cache)"
Q=("what is the capital of france" "how do I bake sourdough bread" "explain how TCP handshakes work" \
   "best way to learn the guitar" "what causes the northern lights" "how do I reset my company email password")
for i in 0 1 2 3 4 5; do echo "  ${Q[$i]}: $(ask "user$i" "${Q[$i]}")"; done

echo "== 2. Load balancing: how many /chat requests each pod served in the last 2 minutes"
kubectl -n $NS logs -l app=gateway --prefix --since=2m 2>/dev/null | grep 'POST /chat' | awk '{print $1}' | sort | uniq -c

echo "== 3. Shared cache: the same question again; expect semantic_cache (score 1.0), whichever pod answers"
echo "  $(ask user5 "how do I reset my company email password")"

echo "== 4. Reworded question; expect semantic_cache (a direct or verifier-confirmed match)"
echo "  $(ask user5 "how can I reset the password for my company email account")"

echo "== 5. Self-healing: delete one gateway pod and watch it come back"
POD=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[0].metadata.name}')
kubectl -n $NS delete pod "$POD" --wait=false
echo "  $(ask alice "what is the tallest mountain in the world")"
sleep 5; kubectl -n $NS get pods


echo
echo "== 6. Config: every gateway pod reads the same ConfigMap file"
for POD in $(kubectl -n $NS get pods -l app=gateway -o name); do
  echo "  $POD: $(kubectl -n $NS exec "$POD" -- grep '^similarity_threshold' /etc/gateway/gateway.config)"
done
echo "  (change smart_ai_gateway/gateway.config, run ./up.sh, and the new value appears in new pods)"

echo
echo "== 7. Cache aging: entries carry hit counts, and the sweep CronJob runs on demand"
kubectl -n $NS exec deploy/gateway -- python -c "
import os
from qdrant_client import QdrantClient
c = QdrantClient(url=os.environ['QDRANT_URL'])
for p in c.scroll('semantic_cache', limit=3)[0]:
    print('  hits=%s  %s' % (p.payload.get('hit_count'), p.payload.get('prompt')))"
for KEY in ttl_seconds sliding_ttl max_age_seconds decay_half_life_seconds; do
  echo "  $(kubectl -n $NS exec deploy/gateway -- grep "^$KEY" /etc/gateway/gateway.config)"
done
kubectl -n $NS delete job cache-sweep-now --ignore-not-found >/dev/null
kubectl -n $NS create job --from=cronjob/cache-sweep cache-sweep-now >/dev/null
if kubectl -n $NS wait --for=condition=complete job/cache-sweep-now --timeout=60s >/dev/null; then
  echo "  $(kubectl -n $NS logs job/cache-sweep-now)"
else
  echo "  sweep job did not finish in 60 s"
fi
kubectl -n $NS delete job cache-sweep-now >/dev/null
