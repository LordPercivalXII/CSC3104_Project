#!/usr/bin/env bash
# Checks the fair-share scheduler across pods: one shared queue, one shared LLM rate limit,
# and recovery when Redis restarts.
#
# Switches the gateway to the mock LLM (no Gemini quota used) with a limit of 2 LLM calls per
# minute, runs the checks, then puts the previous settings back. Takes about 5-10 minutes,
# mostly two rolling restarts while each pod loads its models.
set -uo pipefail
URL=${URL:-http://localhost:8080}
NS=llm-gateway
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

# Wait until the new pods are ready and the old ones have gone (old pods still process
# requests with the old settings while they shut down)
wait_for_gateway() {
  kubectl -n $NS rollout status deploy/gateway --timeout=600s >/dev/null
  while kubectl -n $NS get pods -l app=gateway --no-headers | grep -q Terminating; do sleep 2; done
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

# Pod whose worker processed a request (from its "Processing request <id>" log line)
processed_by() {
  local id
  id=$(grep -o '"request_id":"[^"]*"' "$OUT/$1.json" | cut -d'"' -f4)
  kubectl -n $NS logs -l app=gateway --prefix --tail=-1 --since=10m 2>/dev/null \
    | grep "Processing request ${id:0:8}" | head -1 | sed 's#^\[pod/\([^/]*\)/.*#\1#'
}

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
T0=$(date +%s%N)
send W1 W & send W2 W &
sleep 2
for name in A1 A2 A3; do send "$name" A & sleep 0.3; done
send B1 B &
wait

for name in W1 W2 A1 A2 A3 B1; do
  printf "   %-3s answered after %3s s, processed by %s\n" "$name" "$(secs $name)" "$(processed_by $name)"
done
PODS=$(for name in W1 W2 A1 A2 A3 B1; do processed_by $name; done | sort -u | wc -l)

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
echo "   (requests were processed by $PODS different pod(s); all pods take from the same queue)"

echo
echo "== 3. Redis restart: the gateway keeps running and recovers by itself"
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