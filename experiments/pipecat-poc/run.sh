#!/usr/bin/env bash
# Talk to Friday — M7 Pipecat POC (realtime model, OpenAI-direct).
#
# Usage:
#   ./run.sh               normal run
#   ./run.sh -d            debug (mic levels, VAD events, pipecat logs)
#   ./run.sh --model <name>   model override for this run
#                          (e.g. gpt-realtime-2.1 full model, or the mini)
#
# Env overrides:
#   CHAT_INPUT_DEVICE=<index>   pick a mic
#   CHAT_MODEL=<name>           model override (same as --model)
#   CHAT_MIN_VOLUME=<0..1>      VAD loudness threshold (default 0.15)
set -euo pipefail
cd "$(dirname "$0")"
# --model <name> overrides the model for this run (CHAT_MODEL env)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) export CHAT_MODEL="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ "${1:-}" == "-d" || "${1:-}" == "--debug" ]]; then
  export CHAT_DEBUG=1 PIPECAT_DEBUG=1
fi
exec uv run chat.py