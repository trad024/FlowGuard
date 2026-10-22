#!/usr/bin/env bash
# Starts a canary of podinfo with a good or a bad version (step 2 test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
# A message equal to the stable version's goes straight back to stable, without a canary.
# Usage: ./infra/scripts/deploy-version.sh good|bad [namespace] [message]
set -euo pipefail
VERSION="${1:-}"
NAMESPACE="${2:-staging}"
MESSAGE="${3:-FlowGuard $VERSION build $(date '+%Y-%m-%d %H:%M:%S')}"
case "$VERSION" in good|bad) ;; *) echo "usage: $0 good|bad [namespace] [message]" >&2; exit 2;; esac
case "$NAMESPACE" in staging|prod) ;; *) echo "namespace must be staging or prod" >&2; exit 2;; esac

EXTRA=""
[ "$VERSION" = bad ] && EXTRA=',"--random-error"'
# The message goes through json.dumps so quotes in it stay valid JSON
MSG_JSON="$(printf '%s' "--ui-message=$MESSAGE" | "${PYTHON:-python3}" -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
PATCH="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/command\",\"value\":[\"./podinfo\",\"--port=9898\",\"--level=info\",\"--port-metrics=9797\",$MSG_JSON$EXTRA]}]"

kubectl -n "$NAMESPACE" patch rollout podinfo --type json -p "$PATCH"
echo "Deployed $VERSION version to $NAMESPACE ('$MESSAGE')."
echo "Follow it: kubectl -n $NAMESPACE get rollout podinfo -w"
