#!/usr/bin/env bash
# Runs the scheduler experiments on the kind cluster and saves the raw results
# in eval/results/. Puts the gateway settings back when it finishes or fails.
#
#   bash eval/run_experiments.sh              # mock-only e1 and e3
#   bash eval/run_experiments.sh e1           # or any of: e1 e3 dup
#   RUNS=2 bash eval/run_experiments.sh dup   # fewer repeats
#
# e1   Fairness: light users' wait behind a heavy user. Mock LLM, about 30 min.
# e3   Rate limit: one limit per pod vs one shared limit. Mock LLM, about 20 min.
# dup  Duplicate questions: LLM calls with sharing on vs off. Uses real Gemini
#      (consumes real API quota; check your project limits before running).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1   # repo root
NS=llm-gateway
CONTEXT=${CONTEXT:-kind-llm-gateway}
PY=${PY:-python}
kubectl() { command kubectl --context "$CONTEXT" "$@"; }
RUNS=${RUNS:-3}
RESULTS=${RESULTS:-eval/runs/$(date -u +%Y%m%dT%H%M%SZ)-$$}
EXPERIMENTS=("$@")
if [ ${#EXPERIMENTS[@]} -eq 0 ]; then EXPERIMENTS=(e1 e3); fi
for experiment in "${EXPERIMENTS[@]}"; do
  case "$experiment" in e1|e3|dup) ;; *) echo "Unknown experiment: $experiment"; exit 1 ;; esac
done
mkdir -p "$RESULTS"

VARS=(SCHEDULER_BACKEND LLM_RATE_LIMIT_PER_MINUTE LLM_RATE_LIMIT_MODE GW_LLM_PROVIDER SCHEDULER_COALESCE SCHEDULER_REQUEST_TIMEOUT_SECONDS SCHEDULER_KEY_PREFIX)
PREFIX="eval:$(date -u +%Y%m%dT%H%M%SZ):$$:"

redis() { kubectl -n $NS exec redis-0 -- redis-cli "$@"; }

# Remember the current settings and put them back at the end
kubectl -n "$NS" get deploy gateway -o json | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
names=set(sys.argv[1:])
c=next(c for c in d["spec"]["template"]["spec"]["containers"] if c["name"]=="gateway")
json.dump({"replicas":d["spec"]["replicas"],"env":[e for e in c.get("env",[]) if e["name"] in names]},sys.stdout)
' "${VARS[@]}" > "$RESULTS/original-settings.json"
HPA_MIN=$(kubectl -n $NS get hpa gateway -o jsonpath='{.spec.minReplicas}')
HPA_MAX=$(kubectl -n $NS get hpa gateway -o jsonpath='{.spec.maxReplicas}')
restore() {
  local original_exit=$?
  trap - EXIT INT TERM
  set +e
  local restore_failed=0
  echo
  echo "== Restoring the gateway settings and autoscaler (rolling restart)"
  kubectl -n $NS patch hpa gateway --type=merge \
    -p "{\"spec\":{\"minReplicas\":$HPA_MIN,\"maxReplicas\":$HPA_MAX}}" >/dev/null || restore_failed=1
  "$PY" -c '
import json,sys
saved=json.load(open(sys.argv[1]))
env={e["name"]:e for e in saved["env"]}
entries=[]
for name in sys.argv[2:]:
    if name in env:
        entry=dict(env[name])
        entry["value" if "valueFrom" in entry else "valueFrom"]=None
    else:
        entry={"name":name,"$patch":"delete"}
    entries.append(entry)
json.dump({"spec":{"replicas":saved["replicas"],"template":{"spec":{"containers":[{"name":"gateway","env":entries}]}}}},sys.stdout)
' "$RESULTS/original-settings.json" "${VARS[@]}" > "$RESULTS/restore-patch.json" || restore_failed=1
  kubectl -n $NS patch deploy gateway --type=strategic --patch-file "$RESULTS/restore-patch.json" >/dev/null || restore_failed=1
  kubectl -n $NS rollout status deploy/gateway --timeout=900s || restore_failed=1
  echo "Results: $RESULTS/. Graphs: $PY eval/plot.py --results $RESULTS"
  if [ "$restore_failed" -ne 0 ]; then
    echo "Restoration failed; inspect the cluster and $RESULTS/restore-patch.json" >&2
    if [ "$original_exit" -eq 0 ]; then original_exit=1; fi
  fi
  exit "$original_exit"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Fixed at 3 gateway pods, so the autoscaler cannot change capacity mid-experiment
echo "== Pinning the gateway at 3 pods for the experiments"
kubectl -n $NS patch hpa gateway --type=merge -p '{"spec":{"minReplicas":3,"maxReplicas":3}}' >/dev/null
kubectl -n $NS scale deploy gateway --replicas=3 >/dev/null

wait_ready() {
  local until=$((SECONDS + 900))
  local bounds
  bounds=$(kubectl -n $NS get hpa gateway -o jsonpath='{.spec.minReplicas},{.spec.maxReplicas}')
  [ "$bounds" = "3,3" ] || { echo "HPA is not fixed at 3: $bounds" >&2; return 1; }
  while ! kubectl -n $NS get pods -l app=gateway -o json | "$PY" -c '
import json,sys
pods=json.load(sys.stdin)["items"]
ready=lambda p: not p["metadata"].get("deletionTimestamp") and p["status"].get("phase")=="Running" and any(c["type"]=="Ready" and c["status"]=="True" for c in p["status"].get("conditions",[]))
sys.exit(0 if len(pods)==3 and all(ready(p) for p in pods) else 1)
'; do
    if [ "$SECONDS" -ge "$until" ]; then echo "Timed out waiting for exactly 3 Ready gateways" >&2; return 1; fi
    sleep 2
  done
}
run_load() { wait_ready; "$PY" eval/loadtest.py "$@"; }

# setup <name> <backend> <limit> <provider: mock|config> <coalesce: true|false>
setup() {
  local provider="GW_LLM_PROVIDER=mock"
  if [ "$4" = config ]; then provider="GW_LLM_PROVIDER=gemini"; fi
  echo
  echo "== Setup $1: backend=$2, limit=$3/min, llm=$4, sharing=$5 (rolling restart)"
  kubectl -n $NS set env deploy/gateway "SCHEDULER_BACKEND=$2" \
    "LLM_RATE_LIMIT_PER_MINUTE=$3" "$provider" "SCHEDULER_COALESCE=$5" \
    "LLM_RATE_LIMIT_MODE=sliding" "SCHEDULER_REQUEST_TIMEOUT_SECONDS=600" "SCHEDULER_KEY_PREFIX=$PREFIX" >/dev/null
  kubectl -n $NS rollout status deploy/gateway --timeout=900s >/dev/null
  wait_ready

  local pods
  # Only pods that serve traffic (not ones left over from the rolling restart)
  pods=3
  [ -f "$RESULTS/setups.csv" ] || echo "time,setup,backend,limit,llm,sharing,gateway_pods,commit" > "$RESULTS/setups.csv"
  echo "$(date -Iseconds),$1,$2,$3,$4,$5,$pods,$(git rev-parse --short HEAD)" >> "$RESULTS/setups.csv"
  echo "   $pods gateway pods"
}

# Start each run with an empty rate-limit window. Memory mode keeps its
# limit inside each pod, so there the only way is to wait a full minute.
fresh_window() {
  if [ "$1" = memory ]; then sleep 65; else redis del "${PREFIX}ratelimit" >/dev/null; fi
}

for experiment in "${EXPERIMENTS[@]}"; do
  case $experiment in
    e1)
      # Same total capacity in every setup: 9 LLM calls per minute
      setup fair-shared redis 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window redis
        run_load fairness --config fair-shared --run "$run" --out "$RESULTS/fairness.csv"
        fresh_window redis
        run_load fairness --config fifo --run "$run" --fifo --out "$RESULTS/fairness.csv"
      done
      setup fair-per-pod memory 3 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window memory
        run_load fairness --config fair-per-pod --run "$run" --out "$RESULTS/fairness.csv"
      done
      ;;
    e3)
      # The same setting (9 per minute) as a limit per pod and as one shared limit
      setup limit-per-pod memory 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window memory
        run_load steady --config limit-per-pod --run "$run" --out "$RESULTS/steady.csv"
      done
      setup shared-limit redis 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window redis
        run_load steady --config shared-limit --run "$run" --out "$RESULTS/steady.csv"
      done
      ;;
    dup)
      # Real Gemini at the deployed limit. Never reset the shared limit here:
      # it protects the real Gemini quota, so wait for the window instead.
      for sharing in true false; do
        config="sharing-on"; [ "$sharing" = false ] && config="sharing-off"
        setup "$config" redis 10 config $sharing
        for n in 1 5 10 20; do
          for run in $(seq 1 "$RUNS"); do
            sleep 61
            run_load duplicates --config "$config" --run "$run" --n "$n" --out "$RESULTS/duplicates.csv"
          done
        done
      done
      ;;
    *)
      echo "Unknown experiment '$experiment' (use e1, e3 or dup)"; exit 1 ;;
  esac
done
