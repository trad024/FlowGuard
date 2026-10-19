#!/usr/bin/env bash
# Deletes the FlowGuard cluster and everything in it.
set -euo pipefail
k3d cluster delete flowguard
