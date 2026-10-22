# evaluation

Metric queries over the audit log and Prometheus, comparison reports (agents vs baseline) and
Grafana dashboards. Built in steps 4 and 11.

`loadgen.py` (step 2): fixed-rate load on podinfo with one JSON line per request (time,
namespace, status, latency, pod) in the gitignored `runs/` folder, and a per-pod-template-hash
summary. Standard library only; tests in `tests/`.
