#!/usr/bin/env python3
"""Benchmark dataset collator (M9 #18).

Scans every session report under experiments/*/results/*.json, normalizes
the four report shapes (realtime chat-*, chat_audio audio-/separate-,
pipecat sep-pipecat-*, early probe run-*), and writes:

    docs/benchmarks/sessions.csv   (append-friendly flat CSV)
    docs/benchmarks/sessions.json  (same rows, structured)

Append-friendly: just keep running sessions — rerun this script and the
CSV is refreshed from the JSON sources. Per-turn latency probes
(step1/step2 shape) are intentionally excluded; they are latency
measurements, not sessions.

Usage: uv run python experiments/pipecat-poc/benchmark.py
"""
from __future__ import annotations

import csv
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXP = os.path.join(ROOT, "experiments")
OUT_DIR = os.path.join(ROOT, "docs", "benchmarks")

# Mode + model per report family. Models reflect the preset in effect at
# the time (models.ini); new sessions should record models in their JSON.
FAMILIES = {
    "chat-": dict(mode="realtime", engine="pipecat", model="gpt-realtime-2.1-mini"),
    "audio-": dict(mode="audio", engine="chat_audio", model="gpt-audio-mini"),
    "separate-": dict(mode="separate", engine="chat_audio", model="whisper-local + gpt-4o-mini + piper"),
    "sep-pipecat-": dict(mode="separate", engine="pipecat", model="gpt-4o-mini-transcribe + gpt-4o-mini + gpt-4o-mini-tts"),
}
SKIP_PREFIXES = ("run-",)  # latency probes, not sessions


def row_from(path: str) -> dict | None:
    name = os.path.basename(path)
    if any(name.startswith(p) for p in SKIP_PREFIXES):
        return None
    fam = next((v for k, v in FAMILIES.items() if name.startswith(k)), None)
    if fam is None:
        return None
    try:
        d = json.load(open(path))
    except Exception:
        return None
    if d.get("turns", 0) == 0:  # crashed/debug session
        return None
    if "step1" in d and "turns" not in d:  # probe shape
        return None
    t = d.get("tokens", {}).get("totals") or {}
    cost = d.get("total_cost_usd") or t.get("cost") or 0.0
    wall = d.get("wall_total_s")
    return {
        "ts": d.get("started_utc", name.split("-")[-1].removesuffix(".json").replace("T", " ").replace("Z", "")),
        "mode": fam["mode"],
        "engine": d.get("engine") or fam["engine"],
        "model": fam["model"],
        "turns": d.get("turns", 0),
        "wall_s": round(wall, 1) if wall else "",
        "tok_in": t.get("in", ""),
        "tok_in_audio": t.get("in_audio", ""),
        "tok_cached": t.get("cached", ""),
        "tok_out": t.get("out", ""),
        "tok_out_audio": t.get("out_audio", ""),
        "cost_usd": round(cost, 6) if cost else "",
        "cost_per_turn_usd": round(cost / d["turns"], 6) if cost and d.get("turns") else "",
        "cost_per_min_usd": round(cost / wall, 6) if cost and wall else "",
        "src": os.path.relpath(path, ROOT),
    }


def main() -> None:
    rows = []
    for path in sorted(glob.glob(os.path.join(EXP, "*", "results", "*.json"))):
        r = row_from(path)
        if r:
            rows.append(r)
    os.makedirs(OUT_DIR, exist_ok=True)
    cols = list(rows[0].keys()) if rows else []
    with open(os.path.join(OUT_DIR, "sessions.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(OUT_DIR, "sessions.json"), "w") as f:
        json.dump(rows, f, indent=1)
    print(f"{len(rows)} sessions → docs/benchmarks/sessions.{{csv,json}}")


if __name__ == "__main__":
    main()