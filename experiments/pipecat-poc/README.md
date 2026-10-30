# Pipecat POC (M7) — realtime model wrapped in a minimal Pipecat pipeline

Isolated POC for the M7 scope: check whether **Pipecat + the OpenAI live
model** can work for us, mimic Friday's behavior, answer questions, and
measure output quality and cost. 2-hour timebox.

Companion probes:
- Raw websocket: `../realtime-model-poc/` (the latency/behavior baseline)
- Fallback comparison: `../audio-model-poc/` (footnote only)

## Setup

Environment is project-level via `uv` (venv at `.venv`, Python 3.11 via
`.python-version`); `.env` is git-ignored and holds `OPENAI_API_KEY`.

```bash
uv run --project experiments/pipecat-poc experiments/pipecat-poc/probe.py
```

## Results (2026-10-30, Pipecat 1.15.0, `gpt-realtime-2.1-mini`)

Two runs (`results/run-20261030T090109Z.json`, `run-20261030T090150Z.json`).
Comparison measured the same way as the raw probe: **first audio chunk after
the question audio finished streaming**.

| Metric | Raw websocket probe | Pipecat-wrapped |
|---|---|---|
| First audio after input complete | ~1.7 s | **~1.0–1.4 s** (incl. a 0.3 s artificial pre-commit sleep) |
| Commit → first audio | n/a (manual commit in-line) | **0.69 s / 1.08 s** |
| Tool call mid-voice | ✓ | ✓ (`get_meeting_context` every run) |
| Audio streaming | 12 chunks / 0.65 s span | 10–11 chunks / 0.46–0.48 s span (same burst pattern) |
| Spoken acknowledgement | natural, prompt-driven | natural, prompt-driven |
| Cost per short exchange | ~$0.003 | same model, same rates |

**Framework overhead is not measurable in latency** — the wrapped pipeline is
as fast as (slightly faster than) the hand-rolled websocket, while Pipecat
provides session management, resampling, function-call wiring, interruption
machinery, and metrics infrastructure that the raw probe hand-builds or
omits.

## Key findings

1. **OpenAI's server-side turn detection does not auto-commit** on
   `gpt-realtime-2.1-mini` (pure websocket). Verified at the raw API level
   with four configurations (`semantic_vad`, `semantic_vad+create_response`,
   session default, `server_vad+create_response`, `eagerness: high`): every
   one fires `input_audio_buffer.speech_started` and then nothing — no
   `speech_stopped`, no auto-response. The raw probe only appeared to work
   because it committed manually. **End-of-turn must be detected client-side**
   (Silero VAD / endpointing) — which confirms the conversation-layer plan:
   the realtime model does not remove the VAD/endpointing component.
2. **Pipecat's manual turn mode handles this cleanly**: with
   `turn_detection=False`, the service commits the buffer and sends
   `response.create` when it sees `UserStoppedSpeakingFrame`, and on
   `UserStartedSpeakingFrame` it clears the input buffer and cancels the
   running response (barge-in). Client VAD (Silero, local ONNX) supplies
   those frames in real use.
3. **Pipecat 1.15 gotchas** (cost real debugging time — recorded here to save
   the next session):
   - `PipelineRunner`/`PipelineTask` are deprecated → `PipelineWorker` +
     `WorkerRunner`; frames are injected with `worker.queue_frame`.
   - **Frames are not auto-forwarded**: a processor's `process_frame` must
     explicitly `push_frame` what it wants passed on. An empty passthrough
     source silently swallows StartFrame and the pipeline never starts.
   - `queue_frame` before the pipeline started is queued; the worker logs
     "waiting for StartFrame#0" until the frame reaches the pipeline end.
4. **Conversation memory**: the realtime session holds server-side
   conversation state; multi-turn memory is the session's job, not ours.

## Interactive CLI (chat.py)

`chat.py` — start it and talk to Friday through mic and speakers (same
pipeline as the probe plus a sounddevice transport and Silero VAD), with
per-turn latency and cost printed after every exchange. Run it from inside
this folder:

```bash
./run.sh           # or ./run.sh -d for mic levels, VAD events, logs
./run.sh --model gpt-realtime-2.1   # full model instead of mini
```

Env overrides: `CHAT_INPUT_DEVICE` (mic index), `CHAT_MIN_VOLUME`
(VAD loudness threshold, default 0.15 — this machine's mic reads a loud
noise floor, so tune if VAD never fires). Headphones recommended:
no echo cancellation, so speaker bleed triggers barge-in.

## Timebox

~1 h 20 m used (limit 2 h): ~1 h spent on the two pipecat-1.15 gotchas and
the server-VAD finding, ~20 m on the working probe + docs.