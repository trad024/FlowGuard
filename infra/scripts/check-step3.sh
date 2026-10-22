#!/usr/bin/env bash
# FlowGuard step 3 acceptance check (about 15 to 30 minutes, mostly waiting for CI):
#   1. a commit under app/ runs the app pipeline to success and its image is pushed;
#   2. the laptop deployer picks it up and starts a staging canary with no manual step;
#   3. the canary completes under the L0 analysis (both analyses successful, image promoted);
#   4. a commit touching only docs/ starts no app pipeline run.
# It makes and pushes two small marker commits (app/ci-trigger.txt, docs/ci-trigger.txt) to dev
# under your git identity. It starts its own deployer unless NO_DEPLOYER=1 (use that when one
# already runs). Usage: ./infra/scripts/check-step3.sh   (needs python3)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAMESPACE=staging
PYTHON="${PYTHON:-python3}"
IMAGE_REPO="${FLOWGUARD_IMAGE:-ghcr.io/trad024/flowguard-podinfo}"
CI_TIMEOUT=$(( ${CI_TIMEOUT_MINUTES:-40} * 60 ))
ROLLOUT_TIMEOUT=$(( ${ROLLOUT_TIMEOUT_MINUTES:-25} * 60 ))
failed=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; failed=$((failed+1)); fi; }
field() { kubectl -n "$NAMESPACE" get rollout podinfo -o "jsonpath={$1}"; }
wait_until() { # condition seconds [every]
  local deadline=$((SECONDS + $2))
  while [ "$SECONDS" -lt "$deadline" ]; do eval "$1" >/dev/null 2>&1 && return 0; sleep "${3:-10}"; done
  return 1
}
run_status() { "$PYTHON" "$ROOT/ci/deployer.py" status --sha "$1"; }
json_get() { "$PYTHON" -c 'import json,sys; d=json.loads(sys.argv[1]); print((d or {}).get(sys.argv[2]) or "")' "$1" "$2"; }

cd "$ROOT"
git fetch -q origin dev
[ "$(git rev-parse --abbrev-ref HEAD)" = dev ] || { echo "Check out dev first."; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Working tree not clean; commit or stash first."; exit 1; }
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/dev)" ] || { echo "dev is not in sync with origin/dev."; exit 1; }
[ "$(field .status.phase)" = Healthy ] || { echo "Staging Rollout is not Healthy."; exit 1; }

DEP_PID=""
if [ "${NO_DEPLOYER:-0}" != 1 ]; then
  echo "Starting the deployer (events: evaluation/runs/deployer-events.jsonl)..."
  "$PYTHON" "$ROOT/ci/deployer.py" run >/dev/null 2>&1 &
  DEP_PID=$!
fi
trap '[ -n "$DEP_PID" ] && kill "$DEP_PID" 2>/dev/null' EXIT

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
NOTE="Touched by infra/scripts/check-step3 to test the CI path filters: $STAMP"
printf '%s\n' "$NOTE" > app/ci-trigger.txt
git add app/ci-trigger.txt && git commit -q -m "Step 3 check: app change ($STAMP)"
APP_SHA="$(git rev-parse HEAD)"
printf '%s\n' "$NOTE" > docs/ci-trigger.txt
git add docs/ci-trigger.txt && git commit -q -m "Step 3 check: docs-only change ($STAMP)"
DOCS_SHA="$(git rev-parse HEAD)"
git push -q origin dev || { echo "git push failed"; exit 1; }
echo "Pushed app commit ${APP_SHA:0:7} and docs commit ${DOCS_SHA:0:7}."

# 1. CI for the app commit
echo "Waiting for app-ci on ${APP_SHA:0:7}..."
RUN=""
wait_until 'RUN="$(run_status "$APP_SHA")"; [ "$(json_get "$RUN" status)" = completed ]' "$CI_TIMEOUT" 60
echo "      run: $(json_get "$RUN" html_url)"
check "app commit ran app-ci to success (image pushed)" '[ "$(json_get "$RUN" conclusion)" = success ]'

# 2-3. Deployer -> canary -> promoted under L0
IMAGE="$IMAGE_REPO:$APP_SHA"
STARTED=1
wait_until '[ "$(field ".spec.template.spec.containers[0].image")" = "$IMAGE" ]' 600 && STARTED=0
check "deployer started a staging canary of ${APP_SHA:0:7} with no manual step" '[ "$STARTED" -eq 0 ]'
PROMOTED=1
if [ "$STARTED" -eq 0 ]; then
  wait_until '[ "$(field .status.abort)" != true ] && [ "$(field .status.phase)" = Healthy ] &&
    [ "$(field .status.observedGeneration)" = "$(field .metadata.generation)" ] &&
    [ "$(field .status.stableRS)" = "$(field .status.currentPodHash)" ]' "$ROLLOUT_TIMEOUT" && PROMOTED=0
fi
HASH="$(field .status.currentPodHash)"
RUNS="$(kubectl -n "$NAMESPACE" get analysisrun -l "rollouts-pod-template-hash=$HASH" -o 'jsonpath={.items[*].status.phase}')"
echo "      rollout: $(field .status.phase), stable $(field .status.stableRS), analyses: $RUNS"
if [ "$PROMOTED" -ne 0 ]; then
  PULL="$(kubectl -n "$NAMESPACE" get pods -l "rollouts-pod-template-hash=$HASH" -o 'jsonpath={.items[*].status.containerStatuses[*].state.waiting.reason}')"
  grep -qE 'ImagePull|ErrImage' <<<"$PULL" && echo "      canary pods cannot pull $IMAGE ($PULL): is the GHCR package public?"
fi
check "canary completed under L0 (promoted, 2 successful analyses)" \
  '[ "$PROMOTED" -eq 0 ] && [ "$(grep -o Successful <<<"$RUNS" | wc -l)" -ge 2 ]'

# 4. Docs-only commit: no app-ci run
check "docs-only commit ${DOCS_SHA:0:7} started no app-ci run" '[ "$(run_status "$DOCS_SHA")" = null ]'

echo
echo "Deployer events: $ROOT/evaluation/runs/deployer-events.jsonl"
if [ "$failed" -eq 0 ]; then echo "All step 3 checks passed."; else echo "$failed check(s) failed."; exit 1; fi
