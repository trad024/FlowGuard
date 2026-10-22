#!/usr/bin/env bash
# Optional CI job "integration": runs the built image the way the cluster does and exercises its
# HTTP API and its separate metrics port. Usage: ci/integration-test.sh IMAGE
set -euo pipefail
IMAGE="${1:?usage: $0 IMAGE}"
NAME="podinfo-it-$$"
APP=18898
METRICS=18797
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

docker run -d --name "$NAME" -p "$APP:9898" -p "$METRICS:9797" "$IMAGE" \
  ./podinfo --port=9898 --port-metrics=9797 --level=info >/dev/null

for _ in $(seq 30); do
  curl -fs "http://localhost:$APP/readyz" >/dev/null 2>&1 && break
  sleep 1
done

failed=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; failed=$((failed+1)); fi; }
check "ready"                       "curl -fs http://localhost:$APP/readyz"
check "healthy"                     "curl -fs http://localhost:$APP/healthz"
check "info names the host"         "curl -fs -H 'Accept: application/json' http://localhost:$APP/ | grep -q '\"hostname\"'"
check "version endpoint"            "curl -fs http://localhost:$APP/version | grep -q '\"version\"'"
check "status endpoint returns 500" "[ \"\$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$APP/status/500)\" = 500 ]"
check "metrics port serves request metrics" \
  "curl -fs http://localhost:$METRICS/metrics | grep -q http_request_duration_seconds_count"
check "metrics port liveness"       "curl -fs http://localhost:$METRICS/healthz"

if [ "$failed" -ne 0 ]; then
  docker logs "$NAME" | tail -20
  echo "$failed check(s) failed."
  exit 1
fi
echo "All integration checks passed."
