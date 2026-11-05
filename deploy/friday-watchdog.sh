#!/bin/bash
# Every 5 s: pull Edge streams back from the RDP devices; relaunch Friday only
# if no realtime process exists AND its capture stream is missing from bus 38.
while true; do
  for si in $(pactl list short sink-inputs | awk '$3==246{print $1}'); do
    pactl move-sink-input "$si" 38 2>/dev/null
  done
  for so in $(pactl list short source-outputs | awk '$2==247{print $1}'); do
    pactl move-source-output "$so" 56 2>/dev/null
  done
  if ! pgrep -u abs-worker -f 'bin/python.*chat\.py' >/dev/null \
     && ! pactl list short source-outputs | awk '{print $2}' | grep -q '^38$'; then
    ~/vw-data/friday-realtime.sh
  fi
  sleep 5
done