#!/bin/sh
# vw / vw-admin wrappers: run through the project's uv-managed venv.
REPO="${VW_REPO:-$HOME/virtual-worker}"
export PYTHONPATH="$REPO/common:$REPO/backend:$REPO/agent${PYTHONPATH:+:$PYTHONPATH}"
PY="$REPO/.venv/bin/python3"
[ -x "$PY" ] || PY=python3
case "$(basename "$0")" in
  vw-admin) exec "$PY" -m vw_backend.cli "$@" ;;
  vw)       exec "$PY" "$REPO/agent/bin/vw" "$@" ;;
esac