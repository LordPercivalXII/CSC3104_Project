#!/usr/bin/env bash
# Infrastructure + feature checks: load balancing, shared caches, fair share, self-healing.
URL=${URL:-http://localhost:8080}
NS=llm-gateway

# ask <url> <user> <prompt>  ->  "replica=<pod> source=<redis_cache|semantic_cache|mock_llm>"
ask() {
  local out
  out=$(curl -s -D - -X POST "$1/chat" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$2\",\"team_id\":\"demo\",\"prompt\":\"$3\"}" | tr -d '\r')
  local m; m=$(echo "$out" | grep -o '"matched_prompt":"[^"]*"' | cut -d'"' -f4)
  echo "replica=$(echo "$out" | grep -i '^x-gateway-replica' | awk '{print $2}') source=$(echo "$out" | grep -o '"source":"[^"]*"' | cut -d'"' -f4)${m:+ matched=\"$m\"}"
}
# Start from empty caches so results don't depend on earlier runs.
kubectl -n $NS exec redis-0 -- redis-cli flushall >/dev/null
kubectl -n $NS exec deploy/gateway -- python -c "
import os
from qdrant_client import QdrantClient, models
c = QdrantClient(url=os.environ['QDRANT_URL'])
c.delete('semantic_cache', points_selector=models.FilterSelector(filter=models.Filter(must=[])), wait=True)
" >/dev/null
echo "(caches cleared)"

echo "== 1. Load balancing: 6 different questions should land on different replicas (all mock_llm)"
Q=("what is the capital of france" "how do I bake sourdough bread" "explain how TCP handshakes work" \
   "best way to learn the guitar" "what causes the northern lights" "how do I reset my company email password")
for i in 0 1 2 3 4 5; do ask "$URL" "user$i" "${Q[$i]}"; done

echo "== 2. Shared exact cache: repeat of the last question; expect redis_cache, usually on another replica"
ask "$URL" user5 "how do I reset my company email password"

echo "== 3. Semantic cache: a paraphrase of it; expect semantic_cache with matched=\"how do I reset...\" (if mock_llm, the score was under CACHE_SIMILARITY_THRESHOLD)"
ask "$URL" user5 "how can I reset the password for my company email account"

echo "== 4. Cache stats (exact tier, per the replica that answers)"
curl -s "$URL/cache/stats"; echo

echo "== 5. Fair share on ONE replica: user A sends 3 requests, user B sends 1 just after."
echo "      Expect B to finish before A's last request (round robin), not last (FIFO)."
if [[ -n "${FAIR_URL:-}" ]]; then :; else
  POD=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[0].metadata.name}')
  kubectl -n $NS port-forward "pod/$POD" 18000:8000 >/dev/null 2>&1 &
  PF=$!; FAIR_URL=http://localhost:18000; sleep 3
fi
declare -A FQ=([A1]="how do volcanoes form" [A2]="what is the history of the roman empire" [A3]="how does a bicycle gear work" [B1]="what is photosynthesis")
now_ms() { echo $(( $(date +%s%N) / 1000000 )); }
T0=$(now_ms); PIDS=()
for n in A1 A2 A3; do
  ( ask "$FAIR_URL" userA "${FQ[$n]}" >/dev/null; echo "  $(( $(now_ms) - T0 )) ms  $n done" ) & PIDS+=($!)
done
sleep 0.3
( ask "$FAIR_URL" userB "${FQ[B1]}" >/dev/null; echo "  $(( $(now_ms) - T0 )) ms  B1 done" ) & PIDS+=($!)
wait "${PIDS[@]}"
[[ -n "${PF:-}" ]] && kill "$PF" 2>/dev/null

echo "== 6. Self-healing: delete one gateway pod and watch it come back"
POD=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[0].metadata.name}')
kubectl -n $NS delete pod "$POD" --wait=false
ask "$URL" alice "what is the tallest mountain in the world"
sleep 5; kubectl -n $NS get pods