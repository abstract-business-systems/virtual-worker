#!/usr/bin/env bash
# Talk to Friday on the RAW REALTIME websocket — gpt-realtime-2.1-mini
# direct from OpenAI, no framework; conversation layer hand-built
# (Silero VAD commits the turn, since the server never does).
#
# Usage: ./run-chat-realtime.sh [-d]
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
if [[ "${1:-}" == "-d" || "${1:-}" == "--debug" ]]; then
  export CHAT_DEBUG=1
fi
exec uv run --project ../pipecat-poc chat-realtime.py
