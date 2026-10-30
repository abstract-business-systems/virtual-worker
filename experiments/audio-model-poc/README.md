# M7 POC — OpenAI audio model via OpenRouter (isolated)

Meeting scope: [`docs/recordings/7.vtt`](../../docs/recordings/7.vtt) — check
whether the OpenAI audio model (`gpt-audio-mini` via OpenRouter) can mimic
Friday's behavior: answer questions, call tools, keep conversation memory.
Measure output quality and cost. **Timebox: 2 hours max** (escalate to
Viswanath after 1 hour if stuck).

This folder is self-contained: scripts, fixtures, and results all live here.
Nothing outside `experiments/` is modified.

## Contents

- `probe.py` — the isolated probe (stdlib only; no framework, no worker code)
- `fixtures/` — generated test-question audio
- `results/` — one JSON per run with all measured metrics

## What the probe measures

1. **Bootstrap (TTS direction):** text → audio-out call generates the test
   question. Measures latency and records usage.
2. **Audio-in probe (the make-or-break):** the generated question is sent as
   input audio with a dummy tool, streaming enabled:
   - request → first audio chunk (against the **7.77 s** worker baseline)
   - chunk arrival pattern (smooth stream vs. one blob)
   - tool-call presence in the same response
   - actual token usage (`usage` object → settles the OpenRouter audio rate)

## Results (first run, 2026-10-30)

Verified with `results/run-20261030T074049Z.json` and `ffprobe` durations.
Baseline to beat: **7.77 s** first-audio in the worker.

| Metric | Text→audio (step 1) | Audio-in→audio + tool (step 2) |
|---|---|---|
| **First audio chunk** | **1.22 s** | **1.51 s** (vs 7.77 s baseline → **~80% cut**) |
| Audio delivered | 8.5 s in 16 chunks (~0.84 s span) | 5.9 s in 12 chunks (~0.70 s span) |
| Tool call in same response | — | `get_meeting_context` ✓ (with acknowledgement speech) |
| Cost per call | $0.000417 | $0.0004056 |
| Audio tokens in/out | 0 / 127 | 63 in / 89 out |

Findings:

1. **First-audio latency is ~1.2–1.5 s through OpenRouter streaming** — the
   make-or-break number passes decisively against the 7.77 s baseline.
2. **OpenRouter requires `stream: true` for audio output** (HTTP 400
   otherwise) and, while streaming, **audio format must be `pcm16`** (24 kHz
   mono); WAV is accepted only for `input_audio`. The probe wraps pcm16 into
   WAV locally.
3. **Audio arrives in a few large bursts (<1 s), not a smooth trickle** — the
   first burst already contains seconds of audio, so playback starting at
   ~1.2–1.5 s is comfortably buffered. Chunk granularity is coarse; smooth
   streaming may need the Realtime API or client-side pacing.
4. **Tool calling mid-voice works**: the model acknowledged and issued
   `get_meeting_context` in the same response.
5. **The cheap audio rates are real.** Measured cost ($0.0004056 for the
   audio-in call) matches OpenRouter billing audio at text rates
   ($0.60/M in, $2.40/M out); OpenAI-direct audio rates would be ~6× higher.
   Measured audio density: ~10 tok/s input (9.9), ~15 tok/s output.
6. **Cost at scale (measured density):** ~₹4/meeting-hour (listen 60 min +
   speak ~10 min) → **~₹5,000–5,500/month** at target scale (60
   meeting-hours/day, 22 days) via OpenRouter.
7. Quality note: with a minimal system prompt the model converses naturally
   rather than following scripted lines — behavior is prompt-driven, as M5
   intended.

## Run

```bash
python3 experiments/audio-model-poc/probe.py
```

Requires `OPENROUTER_API_KEY` in the environment (same as the backend).

## Results

Recorded in `results/`; see the run JSON for exact numbers. The demo test
matrix and decision context are in
[`docs/next-steps.md`](../../docs/next-steps.md) ("M6 follow-up").