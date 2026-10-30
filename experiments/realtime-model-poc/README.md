# M7 POC — OpenAI realtime model (isolated, OpenAI-direct)

Meeting scope: [`docs/recordings/7.vtt`](../../docs/recordings/7.vtt) — check
whether Pipecat and the **OpenAI live (realtime) model** can work for us. The
realtime model is **OpenAI-direct only** (websocket Realtime API); it is not
available on OpenRouter. **Timebox: 2 hours max** (escalate to Viswanath
after 1 hour if stuck).

Target model: `gpt-realtime-2.1-mini` (audio $10/M in, $20/M out on OpenAI's
official pricing page). This folder is self-contained; nothing outside
`experiments/` is modified.

## Key setup (dedicated key)

1. Paste the key into `.env` in this folder (`OPENAI_API_KEY=sk-...`).
2. `.env` is git-ignored — it will never be committed.
3. The probe loads `.env` from this folder first; falls back to the
   environment variable.

## What the probe measures

1. **Server-side end-of-turn / VAD** — the model/API's own turn detection
   (the feature the conversation layer would otherwise need hand-built).
2. **First-audio latency** — end of speech → first audio chunk (vs. the
   7.77 s worker baseline and the 1.51 s chat-completions probe).
3. **Interruption/barge-in** — event behavior when speech arrives during
   playback.
4. **Tool calling** — function call inside a realtime voice session.
5. **Actual token usage and cost** — from the API response.

## Results (first run, 2026-10-30)

Verified with `results/run-20261030T080302Z.json` and `ffprobe`.
Baseline to beat: **7.77 s** worker first-audio.

| Metric | Result |
|---|---|
| **First audio chunk** | **2.56 s from probe start (~1.7 s after input complete)** — ~80% cut vs. the 7.77 s baseline |
| Server VAD | `semantic_vad` detected speech start at 1.36 s (works without client-side VAD) |
| Tool call mid-voice | `get_meeting_context` ✓ in the same voice response |
| Spoken acknowledgement | "Okay, let me quickly see what's in your recent meeting and calendar context." — natural, prompt-driven |
| Audio delivered | 4.75 s in 12 chunks spanning 0.65 s (same burst pattern as chat completions) |
| Usage | 412 tokens in (63 audio), 169 out (95 audio, 23 reasoning) |
| Cost per short exchange | ~$0.003 (audio $10/M in, $20/M out, direct) |

Findings:

1. **The realtime model provides its own end-of-turn detection** — the
   `semantic_vad` turn detection worked server-side; no client VAD or
   endpointing needed for this flow.
2. **No inherent realtime speed penalty, with turn detection built in** —
   latency lands in the same ~1.5–1.7 s band as the chat-completions fallback
   (see footnote in [`../audio-model-poc/`](../audio-model-poc/)), but the
   realtime path adds server-side turn detection and interruption that the
   chat-completions path must hand-build.
3. **API shape (GA realtime, 2.1):** session requires `type: "realtime"`,
   nested `audio.input/output.format = {type: "audio/pcm", rate: 24000}`
   (string formats rejected), turn detection under
   `audio.input.turn_detection`, and **no** legacy `OpenAI-Beta` header
   (rejected with `beta_api_shape_disabled`).
4. **Same burst streaming pattern** as chat completions: 12 chunks over
   0.65 s carrying 4.75 s of audio — buffered playback starts fine.
5. **Reasoning tokens appear** (23) in output token details.
6. **Cost at scale (measured density, corrected usage):** the agent listens
   to meetings ~**8 h/day, 5–6 days/week ≈ 190 meeting-hours/month** (not
   24/7). At ~₹51/meeting-hour direct → **~₹9,800/month** — about 12× the
   `gpt-audio-mini`-via-OpenRouter path (~₹780/month), the price of built-in
   turn detection and interruption.

## Run

```bash
uv run --project experiments experiments/realtime-model-poc/probe.py
```

Environment is project-level via `experiments/pyproject.toml` + `uv` (venv at
`experiments/.venv`) — never installed system- or user-wide. Requires the
`websockets` package (already a project dependency) and the key in `.env`.

## Results

One JSON per run in `results/`. Related data: the chat-completions fallback
comparison is in [`../audio-model-poc/`](../audio-model-poc/) (that probe
tested `gpt-audio-mini` via OpenRouter — the cheap fallback path, not the M7
target).