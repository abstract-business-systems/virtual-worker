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
#   realtime — realtime transcript -> shared reasoning -> realtime speech
#   audio    — audio frontend -> shared reasoning -> audio speech (HTTP)
#   separate — dedicated STT -> shared reasoning -> dedicated TTS
#
# Env overrides:
#   CHAT_INPUT_DEVICE=<index>   pick a mic
#   CHAT_MODEL=<name>           voice-model override (same as --model)
#   CHAT_THINK_MODEL=<name>     shared reasoning-model override for ALL modes
#   CHAT_MIN_VOLUME=<0..1>      VAD loudness threshold (default 0.15)
#   CHAT_TEST_WAV=<path>        self-test: feed a wav instead of the mic
set -euo pipefail
INVOKED_FROM="$PWD"   # default bash-tool workdir: where the user launched from
cd "$(dirname "$0")"

show_usage() {
  cat <<'EOF'
Friday — three conversation modes (pipecat POC):

  realtime   realtime speech → shared thinking model → realtime speech
  audio      audio model → shared thinking model → audio model speech
  separate   online STT → shared thinking model → dedicated TTS

Usage: ./run.sh <mode> [-d] [--model <name>] [--folder <path>]
  -d             debug: mic levels, VAD events, api errors
  --model <n>    override the voice/realtime model for this run
  --folder <p>   working directory for the bash tool (default: launch dir)
  --plan <json>  replay a fixture plan without opening the microphone
EOF
}

MODE=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    realtime|audio|separate) MODE="$1"; shift ;;
    --pipecat) ARGS+=("$1"); shift ;;
    --plan|--model|--folder)
      if [[ $# -lt 2 || "$2" == --* ]]; then
        echo "missing value for $1" >&2; exit 2
      fi
      case "$1" in
        --model) export CHAT_MODEL="$2" ;;
        --plan|--folder)
          VALUE="$2"
          [[ "$VALUE" == /* ]] || VALUE="$INVOKED_FROM/$VALUE"
          VALUE="$(realpath -m -- "$VALUE")"
          if [[ "$1" == --plan ]]; then export CHAT_TEST_PLAN="$VALUE"
          else export CHAT_WORKDIR="$VALUE"; fi ;;
      esac
      shift 2 ;;
    -d|--debug) export CHAT_DEBUG=1 PIPECAT_DEBUG=1; shift ;;
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
# Python loaders preserve explicit environment overrides over local secrets.
case "$MODE" in
  realtime) exec uv run chat.py ;;
  audio)    export CHAT_PRESET=audio;    exec uv run chat_audio.py ;;
  separate)
    if [[ " ${ARGS[*]} " == *" --pipecat "* ]] || [[ -n "${CHAT_SEP_PIPECAT:-}" ]]; then
      export CHAT_PRESET=separate; exec uv run chat_sep.py
    fi
    export CHAT_PRESET=separate; exec uv run chat_audio.py ;;
  *) echo "unknown mode: $MODE (use realtime | audio | separate)"; exit 1 ;;
esac