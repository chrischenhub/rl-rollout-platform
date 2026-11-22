#!/bin/bash
# Reference solution.
set -euo pipefail
APP_DIR=${APP_DIR:-/app}
SOLUTION_DIR=$(cd "$(dirname "$0")" && pwd)
cp "$SOLUTION_DIR"/ratelimit/*.py "$APP_DIR/ratelimit/"
cd "$APP_DIR" && python3 -m pytest -q
