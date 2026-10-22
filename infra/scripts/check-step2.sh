#!/usr/bin/env bash
# FlowGuard step 2 acceptance check, run against the live cluster (about 8 minutes):
#   1. a good version goes through 20% -> 50% -> 100% and becomes the stable version;
#   2. during that canary the per-request log shows both stable and canary pods;
#   3. a bad version is aborted by the L0 analysis on its own and stable stays the same;
#   4. after the abort, every request is served by stable pods without errors;
#   5. RAM stays within the 4 GB budget at the canary peak and at the end.
# Usage: ./infra/scripts/check-step2.sh [namespace] [rate]   (needs python3)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS="$ROOT/infra/scripts"
NAMESPACE="${1:-staging}"
RATE="${2:-10}"
PYTHON="${PYTHON:-python3}"
case "$NAMESPACE" in staging) PORT=9898;; prod) PORT=9899;; *) echo "namespace must be staging or prod" >&2; exit 2;; esac
failed=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; failed=$((failed+1)); fi; }
field() { kubectl -n "$NAMESPACE" get rollout podinfo -o "jsonpath={$1}"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%S.000Z; }
wait_until() { # condition seconds
  local deadline=$((SECONDS + $2))
  while [ "$SECONDS" -lt "$deadline" ]; do eval "$1" >/dev/null 2>&1 && return 0; sleep 5; done
  return 1
}
summary() { "$PYTHON" "$ROOT/evaluation/loadgen.py" summary "$LOG" --namespace "$NAMESPACE" --since "$1" --until "$2"; }
# Reads one value from a summary: jq-free, python3 only
get() { "$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2], {"d": d}))' "$1" "$2"; }
show() {
  "$PYTHON" -c 'import json,sys
d = json.loads(sys.argv[2])
print("      %s: %d requests, %d transport errors" % (sys.argv[1], d["requests"], d["transport_errors"]))
for h, v in d["by_hash"].items():
    print("        %-12s %6d requests  %5d 5xx  %5d 4xx" % (h, v["requests"], v["status_5xx"], v["other"]))' "$1" "$2"
}

if ! wait_until '[ "$(field .status.phase)" = Healthy ]' 120; then
  echo "podinfo Rollout in $NAMESPACE is not Healthy. Run ./infra/scripts/setup.sh first."
  exit 1
fi
STABLE0="$(field .status.stableRS)"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
GOOD_MESSAGE="check-step2 $RUN_ID"
LOG="$ROOT/evaluation/runs/step2-$NAMESPACE-$RUN_ID.jsonl"

echo "Load: $RATE requests/s on localhost:$PORT, logged to $LOG"
"$PYTHON" "$ROOT/evaluation/loadgen.py" run --rate "$RATE" \
  --target "$NAMESPACE=http://localhost:$PORT/" --out "$LOG" >/dev/null &
LG_PID=$!
trap 'kill "$LG_PID" 2>/dev/null' EXIT
sleep 15

# 1-2. Good version
echo "Deploying the good version (stable is $STABLE0)..."
T0="$(utc_now)"
"$SCRIPTS/deploy-version.sh" good "$NAMESPACE" "$GOOD_MESSAGE" >/dev/null
wait_until '[ "$(field .status.currentPodHash)" != "$STABLE0" ]' 60
GOOD_HASH="$(field .status.currentPodHash)"
PEAK_EXIT=1
if wait_until '[ "$(field .status.currentStepIndex)" -ge 4 ]' 400; then
  echo "RAM at the 50% canary step:"
  "$SCRIPTS/check-resources.sh"; PEAK_EXIT=$?
fi
PROMOTED=1
wait_until '[ "$(field .status.phase)/$(field .status.stableRS)" = "Healthy/$GOOD_HASH" ]' 600 && PROMOTED=0
S1="$(summary "$T0" "$(utc_now)")"
check "good version $GOOD_HASH promoted to 100% (Healthy, now stable)" '[ "$PROMOTED" -eq 0 ]'
check "per-request log shows stable and canary pods during the canary" \
  '[ "$(get "$S1" "min(d[\"by_hash\"].get(h, {}).get(\"requests\", 0) for h in (\"$STABLE0\", \"$GOOD_HASH\"))")" -gt 0 ]'
show "good canary" "$S1"

# 3-4. Bad version
echo "Deploying the bad version..."
T2="$(utc_now)"
"$SCRIPTS/deploy-version.sh" bad "$NAMESPACE" >/dev/null
wait_until '[ "$(field .status.currentPodHash)" != "$GOOD_HASH" ]' 60
BAD_HASH="$(field .status.currentPodHash)"
ABORTED=1
wait_until '[ "$(field .status.abort)" = true ]' 600 && ABORTED=0
T3="$(utc_now)"
RUNS="$(kubectl -n "$NAMESPACE" get analysisrun -l "rollouts-pod-template-hash=$BAD_HASH" -o 'jsonpath={.items[*].status.phase}')"
echo "      $(field .status.message)"
check "bad version $BAD_HASH aborted by the L0 analysis (AnalysisRun $RUNS)" \
  '[ "$ABORTED" -eq 0 ] && grep -q Failed <<<"$RUNS"'
check "stable version unchanged ($GOOD_HASH)" '[ "$(field .status.stableRS)" = "$GOOD_HASH" ]'
show "bad canary" "$(summary "$T2" "$T3")"
echo "      (unknown = error responses: podinfo's injected errors have no body to name the pod)"

wait_until '[ -z "$(kubectl -n "$NAMESPACE" get pods -l "rollouts-pod-template-hash=$BAD_HASH" -o name)" ]' 120
sleep 5
T4="$(utc_now)"
sleep 20
S4="$(summary "$T4" "$(utc_now)")"
check "stable keeps serving after the abort (all requests on $GOOD_HASH, no errors)" \
  '[ "$(get "$S4" "d[\"requests\"] > 0 and d[\"transport_errors\"] == 0 and list(d[\"by_hash\"]) == [\"$GOOD_HASH\"] and d[\"by_hash\"][\"$GOOD_HASH\"][\"status_5xx\"] + d[\"by_hash\"][\"$GOOD_HASH\"][\"other\"] == 0")" = True ]'
show "after abort" "$S4"

# Back to the stable spec: same template as stable, so no new canary
"$SCRIPTS/deploy-version.sh" good "$NAMESPACE" "$GOOD_MESSAGE" >/dev/null
wait_until '[ "$(field .status.phase)" = Healthy ]' 120
kill "$LG_PID" 2>/dev/null

echo "RAM at the end:"
"$SCRIPTS/check-resources.sh"; END_EXIT=$?
check "RAM within budget at the canary peak and at the end" '[ "$PEAK_EXIT" -eq 0 ] && [ "$END_EXIT" -eq 0 ]'

echo
echo "Per-request log: $LOG"
if [ "$failed" -eq 0 ]; then echo "All step 2 checks passed."; else echo "$failed check(s) failed."; exit 1; fi
