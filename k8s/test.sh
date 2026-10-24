#!/usr/bin/env bash
# Checks: 3 gateway pods + Qdrant, load balancing, shared semantic cache, self-healing.
URL=${URL:-http://localhost:8080}
NS=llm-gateway

# ask <user> <question>  ->  "source=<semantic_cache|mock_llm> score=<similarity>"
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

echo "== 1. Six different questions (all should be mock_llm)"
Q=("what is the capital of france" "how do I bake sourdough bread" "explain how TCP handshakes work" \
   "best way to learn the guitar" "what causes the northern lights" "how do I reset my company email password")
for i in 0 1 2 3 4 5; do echo "  ${Q[$i]}: $(ask "user$i" "${Q[$i]}")"; done

echo "== 2. Load balancing: how many /chat requests each pod served in the last 2 minutes"
kubectl -n $NS logs -l app=gateway --prefix --since=2m 2>/dev/null | grep 'POST /chat' | awk '{print $1}' | sort | uniq -c

echo "== 3. Shared cache: the same question again; expect semantic_cache (score 1.0), whichever pod answers"
echo "  $(ask user5 "how do I reset my company email password")"

echo "== 4. Reworded question; expect semantic_cache (mock_llm means the score was under 0.90)"
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
