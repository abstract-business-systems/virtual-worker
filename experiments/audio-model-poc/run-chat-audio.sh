#!/usr/bin/env bash
# Talk to Friday on the CHEAP path — gpt-audio-mini via OpenRouter,
# hand-built conversation layer (Silero VAD + whole-utterance requests).
#
# Usage: ./run-chat-audio.sh [-d]
set -euo pipefail
cd "$(dirname "$0")"
# key lives in .env (git-ignored), like the other POCs
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "OPENROUTER_API_KEY not set — put it in experiments/audio-model-poc/.env"
  exit 1
fi
if [[ "${1:-}" == "-d" || "${1:-}" == "--debug" ]]; then
  export CHAT_DEBUG=1
fi
exec uv run --project ../pipecat-poc chat-audio.py
