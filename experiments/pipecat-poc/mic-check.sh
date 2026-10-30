#!/usr/bin/env bash
# Microphone diagnostic for the Friday chat POC.
# Usage: ./mic-check.sh [device_index]
set -euo pipefail
cd "$(dirname "$0")"
exec uv run mic-check.py "$@"
