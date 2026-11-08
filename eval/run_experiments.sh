#!/usr/bin/env bash
# Runs the scheduler experiments on the kind cluster and saves the raw results
# in eval/results/. Puts the gateway settings back when it finishes or fails.
#
#   bash eval/run_experiments.sh              # all three (about 1.5 hours)
#   bash eval/run_experiments.sh e1           # or any of: e1 e3 dup
#   RUNS=2 bash eval/run_experiments.sh dup   # fewer repeats
#
# e1   Fairness: light users' wait behind a heavy user. Mock LLM, about 30 min.
# e3   Rate limit: one limit per pod vs one shared limit. Mock LLM, about 20 min.
# dup  Duplicate questions: LLM calls with sharing on vs off. Uses real Gemini
#      (about 120 calls of the 500 per day), about 40 min.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # repo root
NS=llm-gateway
RUNS=${RUNS:-3}
RESULTS=eval/results
EXPERIMENTS=("$@")
[ ${#EXPERIMENTS[@]} -eq 0 ] && EXPERIMENTS=(e1 e3 dup)
mkdir -p "$RESULTS"

VARS=(SCHEDULER_BACKEND LLM_RATE_LIMIT_PER_MINUTE GW_LLM_PROVIDER SCHEDULER_COALESCE)

env_value() {
  kubectl -n $NS get deploy gateway \
    -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name==\"$1\")].value}"
}
redis() { kubectl -n $NS exec redis-0 -- redis-cli "$@"; }

# Remember the current settings and put them back at the end
declare -A ORIGINAL
for v in "${VARS[@]}"; do ORIGINAL[$v]=$(env_value "$v"); done
HPA_MIN=$(kubectl -n $NS get hpa gateway -o jsonpath='{.spec.minReplicas}')
HPA_MAX=$(kubectl -n $NS get hpa gateway -o jsonpath='{.spec.maxReplicas}')
restore() {
  echo
  echo "== Restoring the gateway settings and autoscaler (rolling restart)"
  kubectl -n $NS patch hpa gateway --type=merge \
    -p "{\"spec\":{\"minReplicas\":$HPA_MIN,\"maxReplicas\":$HPA_MAX}}" >/dev/null
  local args=()
  for v in "${VARS[@]}"; do
    if [ -n "${ORIGINAL[$v]}" ]; then args+=("$v=${ORIGINAL[$v]}"); else args+=("$v-"); fi
  done
  kubectl -n $NS set env deploy/gateway "${args[@]}" >/dev/null
  kubectl -n $NS rollout status deploy/gateway --timeout=900s
  echo "Results are in $RESULTS/. Make the graphs with: python eval/plot.py"
}
trap restore EXIT

# Fixed at 3 gateway pods, so the autoscaler cannot change capacity mid-experiment
echo "== Pinning the gateway at 3 pods for the experiments"
kubectl -n $NS patch hpa gateway --type=merge -p '{"spec":{"minReplicas":3,"maxReplicas":3}}' >/dev/null

# setup <name> <backend> <limit> <provider: mock|config> <coalesce: true|false>
setup() {
  local provider="GW_LLM_PROVIDER=mock"
  [ "$4" = config ] && provider="GW_LLM_PROVIDER-"     # use gateway.config (Gemini)
  echo
  echo "== Setup $1: backend=$2, limit=$3/min, llm=$4, sharing=$5 (rolling restart)"
  kubectl -n $NS set env deploy/gateway "SCHEDULER_BACKEND=$2" \
    "LLM_RATE_LIMIT_PER_MINUTE=$3" "$provider" "SCHEDULER_COALESCE=$5" >/dev/null
  kubectl -n $NS rollout status deploy/gateway --timeout=900s >/dev/null
  # Old pods keep working with the old settings until they are gone
  while kubectl -n $NS get pods -l app=gateway --no-headers | grep -q Terminating; do sleep 2; done

  local pods
  # Only pods that serve traffic (not ones left over from the rolling restart)
  pods=$(kubectl -n $NS get pods -l app=gateway --field-selector=status.phase=Running --no-headers | wc -l)
  [ -f "$RESULTS/setups.csv" ] || echo "time,setup,backend,limit,llm,sharing,gateway_pods,commit" > "$RESULTS/setups.csv"
  echo "$(date -Iseconds),$1,$2,$3,$4,$5,$pods,$(git rev-parse --short HEAD)" >> "$RESULTS/setups.csv"
  echo "   $pods gateway pods"
}

# Start each run with an empty rate-limit window. Memory mode keeps its
# limit inside each pod, so there the only way is to wait a full minute.
fresh_window() {
  if [ "$1" = memory ]; then sleep 65; else redis del sched:ratelimit >/dev/null; fi
}

for experiment in "${EXPERIMENTS[@]}"; do
  case $experiment in
    e1)
      # Same total capacity in every setup: 9 LLM calls per minute
      setup fair-shared redis 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window redis
        python3 eval/loadtest.py fairness --config fair-shared --run "$run"
        fresh_window redis
        python3 eval/loadtest.py fairness --config fifo --run "$run" --fifo
      done
      setup fair-per-pod memory 3 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window memory
        python3 eval/loadtest.py fairness --config fair-per-pod --run "$run"
      done
      ;;
    e3)
      # The same setting (9 per minute) as a limit per pod and as one shared limit
      setup limit-per-pod memory 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window memory
        python3 eval/loadtest.py steady --config limit-per-pod --run "$run"
      done
      setup shared-limit redis 9 mock true
      for run in $(seq 1 "$RUNS"); do
        fresh_window redis
        python3 eval/loadtest.py steady --config shared-limit --run "$run"
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
            python3 eval/loadtest.py duplicates --config "$config" --run "$run" --n "$n"
          done
        done
      done
      ;;
    *)
      echo "Unknown experiment '$experiment' (use e1, e3 or dup)"; exit 1 ;;
  esac
done
