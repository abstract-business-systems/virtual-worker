#!/usr/bin/env bash
# Talk to Friday — pipecat-poc with three conversation-layer modes (M8).
#
# Usage:
#   ./run.sh [mode] [-d] [--model <name>] [--folder <path>]
#     mode = realtime | audio | separate     (default: realtime)
#     -d    debug (mic levels, VAD events, api errors)
#     --model <name>   model override for this run
#     --folder <path>  working directory for the bash tool
#                      (default: the directory you launched run.sh from)
#
# Modes (models.ini sections, typed pipelines):
#   realtime — one OpenAI realtime model listens, thinks, speaks (live session)
#   audio    — one audio-model call per turn, OpenRouter (HTTP per turn)
#   separate — separate models: local Whisper STT -> think model -> voice model
#
# Env overrides:
#   CHAT_INPUT_DEVICE=<index>   pick a mic
#   CHAT_MODEL=<name>           model override (same as --model)
#   CHAT_MIN_VOLUME=<0..1>      VAD loudness threshold (default 0.15)
#   CHAT_TEST_WAV=<path>        self-test: feed a wav instead of the mic
set -euo pipefail
INVOKED_FROM="$PWD"   # default bash-tool workdir: where the user launched from
cd "$(dirname "$0")"

show_usage() {
  cat <<'EOF'
Friday — three conversation modes (pipecat POC):

  realtime   one OpenAI realtime model — listens, thinks, speaks (fastest)
  audio      one audio-model call per turn via OpenRouter (cheapest)
  separate   local Whisper STT → think model → voice model (best answers)

Usage: ./run.sh <mode> [-d] [--model <name>] [--folder <path>]
  -d             debug: mic levels, VAD events, api errors
  --model <n>    override the voice/realtime model for this run
  --folder <p>   working directory for the bash tool (default: launch dir)
EOF
}

MODE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    realtime|audio|separate) MODE="$1"; shift ;;
    -d|--debug) export CHAT_DEBUG=1 PIPECAT_DEBUG=1; shift ;;
    --model) export CHAT_MODEL="$2"; shift 2 ;;
    --folder) export CHAT_WORKDIR="$2"; shift 2 ;;
    *) echo "unknown option or mode: $1" >&2; show_usage; exit 1 ;;
  esac
done
if [[ -z "$MODE" ]]; then
  show_usage
  exit 0
fi
export CHAT_WORKDIR="${CHAT_WORKDIR:-$INVOKED_FROM}"
# keys live in .env (git-ignored): OPENAI_API_KEY (realtime) and
# OPENROUTER_API_KEY (audio/separate), like the other POCs
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi
case "$MODE" in
  realtime) exec uv run chat.py ;;
  audio)    export CHAT_PRESET=audio;    exec uv run chat_audio.py ;;
  separate) export CHAT_PRESET=separate; exec uv run chat_audio.py ;;
  *) echo "unknown mode: $MODE (use realtime | audio | separate)"; exit 1 ;;
esac