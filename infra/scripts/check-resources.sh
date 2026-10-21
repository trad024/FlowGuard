#!/usr/bin/env bash
# Measures the cluster's real memory use against the 4 GB budget. Usage: ./infra/scripts/check-resources.sh
set -euo pipefail
BUDGET_MIB=4096
SERVER=k3d-flowguard-server-0
STATS="$(docker stats --no-stream --format '{{.Name}};{{.MemUsage}}' | grep '^k3d-flowguard' || true)"
# The server node must be running; a leftover tools container alone is not a cluster.
if ! grep -q "^$SERVER;" <<<"$STATS"; then
  echo "$SERVER is not running. Run ./infra/scripts/setup.sh first."
  exit 1
fi
python3 -c '
import re, sys
budget = int(sys.argv[1]); total = 0.0
for line in sys.stdin:
    name, usage = line.strip().split(";")
    m = re.match(r"([\d.]+)\s*([KMG])i?B", usage.split("/")[0].strip())
    mib = float(m.group(1)) * {"K": 1/1024, "M": 1, "G": 1024}[m.group(2)] if m else 0
    total += mib; print(f"{name:<28} {mib:8.0f} MiB")
label = "TOTAL cluster"
print(f"{label:<28} {total:8.0f} MiB  (budget {budget} MiB)")
print("OVER BUDGET" if total > budget else "Within budget.")
sys.exit(1 if total > budget else 0)
' "$BUDGET_MIB" <<<"$STATS"
