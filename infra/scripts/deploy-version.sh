#!/usr/bin/env bash
# Starts a canary of podinfo with a good, bad or fault version (test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
#   fault: graded fault (D19, needs a FlowGuard-built image), set with FAULT_ERROR_RATE,
#         FAULT_LATENCY_MS, FAULT_JITTER_MS, FAULT_FRACTION (defaults 0, 0, 0, 1). User routes
#         only; probes and the metrics port are untouched.
# Every version gets a new release Secret (D25), podinfo-release-<random>, mounted with envFrom:
# the fault settings for "fault", empty values otherwise. The pod spec shows only its name.
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
RATE="" LATENCY="" JITTER="" FRACTION=""
if [ "$VERSION" = fault ]; then
  RATE="${FAULT_ERROR_RATE:-0}" LATENCY="${FAULT_LATENCY_MS:-0}"
  JITTER="${FAULT_JITTER_MS:-0}" FRACTION="${FAULT_FRACTION:-1}"
fi
RELEASE="podinfo-release-$(od -An -N4 -tx1 /dev/urandom | tr -d ' 
')"
kubectl -n "$NAMESPACE" create secret generic "$RELEASE"   --from-literal=PODINFO_FAULT_ERROR_RATE="$RATE" --from-literal=PODINFO_FAULT_LATENCY_MS="$LATENCY"   --from-literal=PODINFO_FAULT_JITTER_MS="$JITTER" --from-literal=PODINFO_FAULT_FRACTION="$FRACTION"   --dry-run=client -o yaml   | kubectl label --local -f - -o yaml release-settings=podinfo   | kubectl apply -f - >/dev/null
# The message goes through json.dumps so quotes in it stay valid JSON
MSG_JSON="$(printf '%s' "--ui-message=$MESSAGE" | "${PYTHON:-python3}" -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
PATCH="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/command\",\"value\":[\"./podinfo\",\"--port=9898\",\"--level=info\",\"--port-metrics=9797\",$MSG_JSON$EXTRA]}"
PATCH="$PATCH,{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/envFrom\",\"value\":[{\"secretRef\":{\"name\":\"$RELEASE\"}}]}"
if [ -n "$IMAGE" ]; then
  PATCH="$PATCH,{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"$IMAGE\"}"
fi
PATCH="$PATCH]"

kubectl -n "$NAMESPACE" patch rollout podinfo --type json -p "$PATCH"
echo "Deployed $VERSION version to $NAMESPACE ('$MESSAGE'${IMAGE:+, image $IMAGE}, settings $RELEASE)."
echo "Follow it: kubectl -n $NAMESPACE get rollout podinfo -w"
