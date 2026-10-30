#!/usr/bin/env bash
# Talk to Friday on the RAW REALTIME websocket — gpt-realtime-2.1-mini
# direct from OpenAI, no framework; conversation layer hand-built
# (Silero VAD commits the turn, since the server never does).
#
# Usage: ./run.sh [-d] [--model gpt-realtime-2.1|gpt-realtime-2.1-mini]
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
# --model <name> overrides the model for this run (CHAT_MODEL env)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) export CHAT_MODEL="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ "${1:-}" == "-d" || "${1:-}" == "--debug" ]]; then
  export CHAT_DEBUG=1
fi
exec uv run --project ../pipecat-poc chat-realtime.py
