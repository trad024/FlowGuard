#!/usr/bin/env bash
# Optional CI job "integration": runs the built image the way the cluster does and exercises its
# HTTP API and its separate metrics port. Usage: ci/integration-test.sh IMAGE
set -euo pipefail
IMAGE="${1:?usage: $0 IMAGE}"
NAME="podinfo-it-$$"
APP=18898
METRICS=18797
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

# In GitHub Actions, failures also become annotations, readable without access to the logs
annotate() {
  [ -n "${GITHUB_ACTIONS:-}" ] || return 0
  local m="${1//'%'/'%25'}"
  m="${m//$'\r'/'%0D'}"
  m="${m//$'\n'/'%0A'}"
  echo "::error title=integration::$m"
}

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  annotate "image $IMAGE not found; images: $(docker images --format '{{.Repository}}:{{.Tag}}' | head -5)"
  echo "Image $IMAGE not found."
  exit 1
fi
docker run -d --name "$NAME" -p "$APP:9898" -p "$METRICS:9797" "$IMAGE" \
  ./podinfo --port=9898 --port-metrics=9797 --level=info >/dev/null

for _ in $(seq 30); do
  curl -fs "http://localhost:$APP/readyz" >/dev/null 2>&1 && break
  sleep 1
done

failed=0
check() {
  local out
  if out="$(eval "$2" 2>&1)"; then
    echo "PASS  $1"
  else
    echo "FAIL  $1"
    annotate "FAIL $1: $(printf '%s' "$out" | tail -c 300)"
    failed=$((failed+1))
  fi
}
check "ready"                       "curl -fsS http://localhost:$APP/readyz"
check "healthy"                     "curl -fsS http://localhost:$APP/healthz"
check "info names the host"         "curl -fsS -H 'Accept: application/json' http://localhost:$APP/ | grep '\"hostname\"'"
check "version endpoint"            "curl -fsS http://localhost:$APP/version | grep '\"version\"'"
check "status endpoint returns 500" "[ \"\$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:$APP/status/500)\" = 500 ]"
check "metrics port serves request metrics" \
  "curl -fsS http://localhost:$METRICS/metrics | grep -c http_request_duration_seconds_count"
check "metrics port liveness"       "curl -fsS http://localhost:$METRICS/healthz"

if [ "$failed" -ne 0 ]; then
  state="$(docker ps -a --filter "name=$NAME" --format '{{.Status}} {{.Ports}}')"
  logs="$(docker logs "$NAME" 2>&1 | tail -8)"
  echo "container: $state"
  echo "$logs"
  annotate "container: $state; last log lines: $logs"
  echo "$failed check(s) failed."
  exit 1
fi
echo "All integration checks passed."
