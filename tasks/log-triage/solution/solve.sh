#!/bin/bash
# Reference solution: the triage script, its report, and a written summary.
set -euo pipefail
APP_DIR=${APP_DIR:-/app}
SOLUTION_DIR=$(cd "$(dirname "$0")" && pwd)
cp "$SOLUTION_DIR/triage.py" "$APP_DIR/triage.py"
python3 "$APP_DIR/triage.py" --logs "$APP_DIR/logs" --out "$APP_DIR/out"
cp "$SOLUTION_DIR/summary.md" "$APP_DIR/out/summary.md"
