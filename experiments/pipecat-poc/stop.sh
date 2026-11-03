#!/usr/bin/env bash
# Kill all running virtual-worker conversation POC instances
# (realtime / audio / separate CLIs). Safe to run anytime: if nothing is
# running it does nothing.
set -uo pipefail

# graceful first (SIGINT -> clean shutdown, session report still written)
pkill -INT -f "experiments/pipecat-poc/chat\.py"            2>/dev/null
pkill -INT -f "experiments/pipecat-poc/chat_audio\.py"      2>/dev/null
pkill -INT -f "experiments/realtime-model-poc/chat-realtime\.py" 2>/dev/null
pkill -INT -f "experiments/audio-model-poc/chat-audio\.py"  2>/dev/null
sleep 2

# then force anything that survived
pkill -f "experiments/pipecat-poc/chat\.py"            2>/dev/null
pkill -f "experiments/pipecat-poc/chat_audio\.py"      2>/dev/null
pkill -f "experiments/realtime-model-poc/chat-realtime\.py" 2>/dev/null
pkill -f "experiments/audio-model-poc/chat-audio\.py"  2>/dev/null
sleep 1

if pgrep -f "experiments/(pipecat-poc|realtime-model-poc|audio-model-poc)/chat" >/dev/null 2>&1; then
  echo "WARNING: some instances survived — check: pgrep -af 'chat.*py'"
  exit 1
fi
echo "all virtual-worker conversation instances stopped"