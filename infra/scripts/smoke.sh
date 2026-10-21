#!/usr/bin/env bash
# FlowGuard step 1 acceptance check. Usage: ./infra/scripts/smoke.sh  (needs curl and python3)
set -uo pipefail
failed=0
check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; failed=$((failed+1)); fi; }
prom() { curl -fsG http://localhost:9090/api/v1/query --data-urlencode "query=$1" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "")'; }

# Stop early with a clear message when the cluster is not up.
if ! curl -fs --max-time 5 http://localhost:9898/healthz >/dev/null; then
  echo "Cluster not reachable on localhost:9898. Run ./infra/scripts/setup.sh first."
  exit 1
fi

echo "Sending 60 requests to each podinfo so metrics exist..."
for port in 9898 9899; do for _ in $(seq 60); do curl -fs "http://localhost:$port/" >/dev/null; done; done
sleep 35

check "podinfo staging answers"  'curl -fs http://localhost:9898/healthz'
check "podinfo prod answers"     'curl -fs http://localhost:9899/healthz'
check "Prometheus ready"         'curl -fs http://localhost:9090/-/ready'
check "Alertmanager ready"       'curl -fs http://localhost:9093/-/ready'
check "Grafana healthy"          'curl -fs http://localhost:3000/api/health | grep -q "\"ok\""'
check "podinfo scraped in both namespaces" '[ "$(prom "count(up{app=\"podinfo\"} == 1)")" = "2" ]'
check "podinfo request metrics present"    '[ "$(prom "count(sum by (namespace) (http_requests_total{app=\"podinfo\"}))")" = "2" ]'
check "kube-state-metrics scraped"         '[ "$(prom "up{job=\"kube-state-metrics\"}")" = "1" ]'
check "container memory metrics present in both namespaces" \
  '[ "$(prom "count(sum by (namespace) (container_memory_working_set_bytes{container=\"podinfo\"}))")" = "2" ]'

echo
if [ "$failed" -eq 0 ]; then echo "All checks passed. Open Grafana > FlowGuard > podinfo."; else echo "$failed check(s) failed."; exit 1; fi
