#!/bin/sh
# vw / vw-admin wrappers: put the repo tree on PYTHONPATH and run.
REPO=/opt/virtual-worker
export PYTHONPATH="$REPO/common:$REPO/backend:$REPO/agent${PYTHONPATH:+:$PYTHONPATH}"
case "$(basename "$0")" in
  vw-admin) exec python3 -m vw_backend.cli "$@" ;;
  vw)       exec python3 "$REPO/agent/bin/vw" "$@" ;;
esac