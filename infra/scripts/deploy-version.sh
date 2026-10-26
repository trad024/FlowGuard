#!/usr/bin/env bash
# Starts a canary of podinfo with a good, bad or fault version (test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
#   fault: graded fault (D19, needs a FlowGuard-built image), set with FAULT_ERROR_RATE,
#         FAULT_LATENCY_MS, FAULT_JITTER_MS, FAULT_FRACTION (defaults 0, 0, 0, 1). User routes
#         only; probes and the metrics port are untouched.
# An image argument also switches the container image (the CI deployer passes the image built
# for a commit). A message equal to the stable version's goes straight back to stable.
# Usage: ./infra/scripts/deploy-version.sh good|bad [namespace] [message] [image]
set -euo pipefail
VERSION="${1:-}"
NAMESPACE="${2:-staging}"
MESSAGE="${3:-FlowGuard $VERSION build $(date '+%Y-%m-%d %H:%M:%S')}"
IMAGE="${4:-}"
case "$VERSION" in good|bad|fault) ;; *) echo "usage: $0 good|bad|fault [namespace] [message] [image]" >&2; exit 2;; esac
case "$NAMESPACE" in staging|prod) ;; *) echo "namespace must be staging or prod" >&2; exit 2;; esac

EXTRA=""
[ "$VERSION" = bad ] && EXTRA=',"--random-error"'
if [ "$VERSION" = fault ]; then
  EXTRA=",\"--fault-error-rate=${FAULT_ERROR_RATE:-0}\",\"--fault-latency-ms=${FAULT_LATENCY_MS:-0}\""
  EXTRA="$EXTRA,\"--fault-jitter-ms=${FAULT_JITTER_MS:-0}\",\"--fault-fraction=${FAULT_FRACTION:-1}\""
fi
# The message goes through json.dumps so quotes in it stay valid JSON
MSG_JSON="$(printf '%s' "--ui-message=$MESSAGE" | "${PYTHON:-python3}" -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
PATCH="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/command\",\"value\":[\"./podinfo\",\"--port=9898\",\"--level=info\",\"--port-metrics=9797\",$MSG_JSON$EXTRA]}"
if [ -n "$IMAGE" ]; then
  PATCH="$PATCH,{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"$IMAGE\"}"
fi
PATCH="$PATCH]"

kubectl -n "$NAMESPACE" patch rollout podinfo --type json -p "$PATCH"
echo "Deployed $VERSION version to $NAMESPACE ('$MESSAGE'${IMAGE:+, image $IMAGE})."
echo "Follow it: kubectl -n $NAMESPACE get rollout podinfo -w"
