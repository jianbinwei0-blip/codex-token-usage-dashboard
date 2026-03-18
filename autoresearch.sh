#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
RUNTIME_HTML="$REPO_ROOT/tmp/autoresearch.runtime.html"

mkdir -p "$REPO_ROOT/tmp"
export AI_USAGE_DASHBOARD_HTML="$RUNTIME_HTML"
/usr/bin/python3 "$REPO_ROOT/scripts/seed_runtime_html.py" "$REPO_ROOT/dashboard/index.html" "$AI_USAGE_DASHBOARD_HTML"

exec /usr/bin/python3 "$REPO_ROOT/scripts/benchmark_live_recalc.py" --repeat 5
