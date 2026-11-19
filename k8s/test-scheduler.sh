#!/usr/bin/env bash
# Checks the fair-share scheduler across pods: one shared queue, one shared LLM rate limit,
# one LLM call for duplicate questions, and recovery when Redis restarts.
#
# Switches the gateway to the mock LLM (no Gemini quota used) with a limit of 2 LLM calls per
# minute, runs the checks, then puts the previous settings back. Takes about 5-10 minutes,
# mostly two rolling restarts while each pod loads its models.
set -uo pipefail
URL=${URL:-http://localhost:8080}
NS=llm-gateway
# Git Bash on Windows rewrites /paths inside kubectl exec commands (/etc/... -> C:/Program Files/Git/etc/...)
export MSYS_NO_PATHCONV=1
TEST_LIMIT=2
OUT=$(mktemp -d)
FAILED=0

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAILED=1; }

# Value of an environment variable in the gateway Deployment ("" if not set)
env_value() {
  kubectl -n $NS get deploy gateway \
    -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name==\"$1\")].value}"
}

# Wait until every gateway pod runs the current settings: the rollout is complete, no old
# pod is still shutting down (old pods keep processing requests with the old settings), and
# the number of pods equals the number the Deployment has updated.
wait_for_gateway() {
  kubectl -n $NS rollout status deploy/gateway --timeout=600s >/dev/null
  local until=$((SECONDS + 600)) want have
  while true; do
    want=$(kubectl -n $NS get deploy gateway -o jsonpath='{.status.updatedReplicas}')
    have=$(kubectl -n $NS get pods -l app=gateway --no-headers 2>/dev/null | grep -c ' Running ')
    if ! kubectl -n $NS get pods -l app=gateway --no-headers | grep -q Terminating \
       && [ -n "$want" ] && [ "$have" = "$want" ] \
       && [ "$(kubectl -n $NS get deploy gateway -o jsonpath='{.status.replicas}')" = "$want" ]; then
      return 0
    fi
    [ "$SECONDS" -ge "$until" ] && { echo "  gateway pods did not settle"; return 1; }
    sleep 2
  done
}

redis() { kubectl -n $NS exec redis-0 -- redis-cli "$@"; }

# send <name> <user>: one request to /scheduler/chat; saves the response and the finish
# time in seconds after T0
send() {
  curl -s -X POST "$URL/scheduler/chat" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$2\",\"prompt\":\"$1\"}" > "$OUT/$1.json"
  echo $(( ($(date +%s%N) - T0) / 1000000000 )) > "$OUT/$1.s"
}
secs() { cat "$OUT/$1.s"; }

# ask <name> <user> <question>: one request to /chat (cache + scheduler), response saved
ask() {
  curl -s -X POST "$URL/chat" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$2\",\"prompt\":\"$3\"}" > "$OUT/$1.json"
}

# LLM calls made so far by each gateway pod, as "<pod> <count>" lines, read from each pod's
# Prometheus counter gateway_inference_calls_total (successful and failed calls).
pod_calls() {
  local pod
  for pod in $(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[*].metadata.name}'); do
    echo "$pod $(kubectl -n $NS exec "$pod" -c gateway -- python -c '
import urllib.request
text = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5).read().decode()
print(int(sum(float(line.rsplit(" ", 1)[1]) for line in text.splitlines()
              if line.startswith("gateway_inference_calls_total{"))))' 2>/dev/null || echo 0)"
  done
}

# Total LLM calls made so far by all gateway pods
llm_calls() { pod_calls | awk '{s += $2} END {print s + 0}'; }

# Number of pods whose LLM-call count grew between two pod_calls snapshots
pods_used() { join <(sort "$1") <(sort "$2") | awk '$3 > $2' | wc -l; }

echo "== 0. Redis is running with the expected settings"
kubectl -n $NS get pods
if [ "$(env_value SCHEDULER_BACKEND)" != redis ]; then
  echo "  SCHEDULER_BACKEND is not redis in deploy/gateway. Deploy k8s/ first (k8s/up.sh)."
  exit 1
fi
[ "$(redis ping)" = PONG ] && pass "redis-0 answers PING" || fail "redis-0 does not answer PING"
[ "$(redis config get maxmemory-policy | tail -1)" = volatile-lru ] \
  && pass "maxmemory-policy is volatile-lru" || fail "maxmemory-policy is not volatile-lru"
[ "$(redis config get appendonly | tail -1)" = yes ] \
  && pass "appendonly is on" || fail "appendonly is off"

ORIG_LIMIT=$(env_value LLM_RATE_LIMIT_PER_MINUTE)
ORIG_PROVIDER=$(env_value GW_LLM_PROVIDER)

# Put the gateway back as it was, whatever happens below
restore() {
  echo
  echo "== Restoring the gateway settings (rolling restart)"
  local limit provider
  if [ -n "$ORIG_LIMIT" ]; then limit="LLM_RATE_LIMIT_PER_MINUTE=$ORIG_LIMIT"; else limit="LLM_RATE_LIMIT_PER_MINUTE-"; fi
  if [ -n "$ORIG_PROVIDER" ]; then provider="GW_LLM_PROVIDER=$ORIG_PROVIDER"; else provider="GW_LLM_PROVIDER-"; fi
  kubectl -n $NS set env deploy/gateway "$limit" "$provider" >/dev/null
  kubectl -n $NS rollout status deploy/gateway --timeout=600s
  rm -rf "$OUT"
  echo
  if [ $FAILED -eq 0 ]; then echo "All scheduler checks passed."; else echo "Some scheduler checks FAILED."; fi
  exit $FAILED
}
trap restore EXIT

echo
echo "== 1. Switching to the mock LLM with a limit of $TEST_LIMIT LLM calls per minute (rolling restart)"
kubectl -n $NS set env deploy/gateway GW_LLM_PROVIDER=mock "LLM_RATE_LIMIT_PER_MINUTE=$TEST_LIMIT" >/dev/null
wait_for_gateway
kubectl -n $NS get pods -l app=gateway
redis del sched:ratelimit >/dev/null      # start from an empty rate-limit window

echo
echo "== 2. One rate limit and one fair queue for all pods (about 2 minutes)"
echo "   W sends 2 requests (this minute's limit), then A sends 3 and B sends 1."
echo "   Expected: W1 W2 now; A1 and B1 about 60 s later; A2 and A3 about 120 s later."
echo "   With a limit per pod, 3 pods would allow 6 calls at once; with plain first come,"
echo "   first served, B1 would wait behind all of A's requests."
pod_calls > "$OUT/calls-before"
T0=$(date +%s%N)
send W1 W & send W2 W &
sleep 2
for name in A1 A2 A3; do send "$name" A & sleep 0.3; done
send B1 B &
wait
pod_calls > "$OUT/calls-after"

for name in W1 W2 A1 A2 A3 B1; do
  printf "   %-3s answered after %3s s\n" "$name" "$(secs $name)"
done
PODS=$(pods_used "$OUT/calls-before" "$OUT/calls-after")

[ "$(secs W1)" -lt 10 ] && [ "$(secs W2)" -lt 10 ] \
  && pass "the first $TEST_LIMIT requests ran straight away" \
  || fail "the first $TEST_LIMIT requests were held back"
[ "$(secs A1)" -ge 45 ] && [ "$(secs B1)" -ge 45 ] \
  && pass "no more than $TEST_LIMIT calls started in the first minute, across all pods" \
  || fail "more than $TEST_LIMIT calls started in the first minute"
[ "$(secs B1)" -lt "$(secs A2)" ] && [ "$(secs B1)" -lt "$(secs A3)" ] \
  && pass "B1 went ahead of A2 and A3 (fair share)" \
  || fail "B1 waited behind A's requests"
[ "$(secs A2)" -ge 105 ] && [ "$(secs A3)" -ge 105 ] \
  && pass "A2 and A3 waited for the next minute" \
  || fail "A2 or A3 started too early"
echo "   (LLM calls ran on $PODS different pod(s); all pods take from the same queue)"

echo
echo "== 3. Duplicate questions: 5 people ask the same new question at the same time (about 1 minute)"
echo "   Step 2 used up this minute's calls, so all 5 arrive while the first one waits its turn."
echo "   Expected: 1 LLM call; the other 4 share its answer and are not cached again."
# A fresh question each run, without digits: a long number would be read as personal data
# (a phone number), and personal prompts are deliberately never shared or cached.
QUESTION="How do I request access to the shared drive for team $(LC_ALL=C tr -dc 'a-z' < /dev/urandom | head -c 8)?"
BEFORE=$(llm_calls)
for i in 1 2 3 4 5; do ask "D$i" "dup-user$i" "$QUESTION" & done
wait
CALLS=$(( $(llm_calls) - BEFORE ))
# Count files, not lines: each response is one JSON line without a newline at the end
ANSWERED=$(grep -l '"response"' "$OUT"/D?.json | wc -l)
SHARED=$(grep -l '"coalesced":true' "$OUT"/D?.json | wc -l)
echo "   $ANSWERED answered, $SHARED shared an answer, $CALLS LLM call(s)"
[ "$ANSWERED" -eq 5 ] && pass "all 5 people got an answer" || fail "only $ANSWERED of 5 got an answer"
[ "$CALLS" -eq 1 ] && pass "only 1 LLM call for 5 identical questions" || fail "$CALLS LLM calls for 5 identical questions"
[ "$SHARED" -eq 4 ] && pass "4 answers were shared from that call" || fail "$SHARED answers were shared, expected 4"

echo
echo "== 4. Redis restart: the gateway keeps running and recovers by itself"
RESTARTS_BEFORE=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[*].status.containerStatuses[0].restartCount}')
redis set sched:test:persist ok >/dev/null           # no TTL, so it must survive on the volume
kubectl -n $NS delete pod redis-0 >/dev/null
echo "   deleted redis-0"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$URL/scheduler/chat" \
  -H 'Content-Type: application/json' -d '{"user_id":"R","prompt":"during restart"}')
echo "   request while Redis is restarting: HTTP $CODE (an error is expected here)"
kubectl -n $NS wait --for=condition=Ready pod/redis-0 --timeout=120s >/dev/null
redis del sched:ratelimit >/dev/null     # step 2 used up this minute's calls, and the limit survives restarts

CODE=000
for _ in $(seq 1 15); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$URL/scheduler/chat" \
    -H 'Content-Type: application/json' -d '{"user_id":"R","prompt":"after restart"}')
  [ "$CODE" = 200 ] && break
  sleep 2
done
[ "$CODE" = 200 ] && pass "requests work again after Redis is back" \
  || fail "requests still fail after Redis is back (HTTP $CODE)"
[ "$(redis get sched:test:persist)" = ok ] && pass "data on the volume survived the restart" \
  || fail "data was lost in the restart"
redis del sched:test:persist >/dev/null
RESTARTS_AFTER=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[*].status.containerStatuses[0].restartCount}')
[ "$RESTARTS_BEFORE" = "$RESTARTS_AFTER" ] && pass "no gateway pod restarted" \
  || fail "gateway pods restarted ($RESTARTS_BEFORE -> $RESTARTS_AFTER)"
kubectl -n $NS get pods
