#!/bin/bash
# Reference solution: the fixed modules and a CHANGES.md.
set -euo pipefail
APP_DIR=${APP_DIR:-/app}
SOLUTION_DIR=$(cd "$(dirname "$0")" && pwd)
cp "$SOLUTION_DIR"/ledger/*.py "$APP_DIR/ledger/"
cp "$SOLUTION_DIR/CHANGES.md" "$APP_DIR/CHANGES.md"
cd "$APP_DIR" && python3 -m pytest -q
