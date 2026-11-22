#!/bin/bash
# Sync code to the server and rebuild. `--tasks` also syncs and re-imports ./tasks.
# Restarting the worker is safe mid-job: it stops gracefully and its runs are retried.
#   DEPLOY_HOST=root@203.0.113.10 SITE_ADDRESS=203-0-113-10.sslip.io ./deploy.sh --tasks
set -euo pipefail
cd "$(dirname "$0")"
HOST=${DEPLOY_HOST:?set DEPLOY_HOST, e.g. root@203.0.113.10}
SITE=${SITE_ADDRESS:?set SITE_ADDRESS to the public hostname}
KEY=${DEPLOY_KEY:-$HOME/.ssh/id_ed25519}
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")
COMPOSE="SITE_ADDRESS=$SITE docker compose -f compose.yaml -f compose.prod.yaml --profile app"

rsync -az --delete -e "ssh -i $KEY" \
  --exclude .git --exclude .venv --exclude data --exclude .env --exclude secrets.env \
  --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store --exclude tasks \
  ./ "$HOST:/opt/rollout/platform/"
if [[ "${1:-}" == "--tasks" ]]; then
  rsync -az --delete -e "ssh -i $KEY" --exclude .DS_Store --exclude __pycache__ \
    tasks/ "$HOST:/opt/rollout/tasks/"
fi

"${SSH[@]}" "cd /opt/rollout/platform && $COMPOSE up -d --build --remove-orphans"
if [[ "${1:-}" == "--tasks" ]]; then
  "${SSH[@]}" "cd /opt/rollout/platform && $COMPOSE run --rm -T -v /opt/rollout/tasks:/tasks:ro api rollout import-tasks /tasks"
fi
"${SSH[@]}" "cd /opt/rollout/platform && $COMPOSE ps --format 'table {{.Service}}\t{{.Status}}'"
