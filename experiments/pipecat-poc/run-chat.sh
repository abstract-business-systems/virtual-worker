#!/usr/bin/env bash
# Talk to Friday — M7 Pipecat POC (realtime model, OpenAI-direct).
#
# Usage:
#   ./run-chat.sh          normal run
#   ./run-chat.sh -d       debug (mic levels, VAD events, pipecat logs)
#
# Env overrides:
#   CHAT_INPUT_DEVICE=<index>   pick a mic (see ./run-chat.sh -d header line)
#   CHAT_MIN_VOLUME=<0..1>      VAD loudness threshold (default 0.15)
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "-d" || "${1:-}" == "--debug" ]]; then
  export CHAT_DEBUG=1 PIPECAT_DEBUG=1
fi
exec uv run chat.py
