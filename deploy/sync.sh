#!/bin/bash
# Sync the working tree to the isolated abs-worker account on this host.
# Usage (as your normal user): deploy/sync.sh
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete \
      --exclude .git --exclude 'deploy/keys' --exclude '*.log' \
      --exclude __pycache__ --exclude .venv --exclude '*.pyc' \
      "$SRC/" abs-worker@localhost:virtual-worker/
echo "synced to abs-worker@localhost:~/virtual-worker"
echo "re-run bootstrap if deps/units changed:"
echo "  ssh abs-worker@localhost 'bash -s' < deploy/bootstrap-abs-worker.sh"