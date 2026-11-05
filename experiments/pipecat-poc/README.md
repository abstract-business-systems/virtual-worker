# Pipecat POC — three conversation-layer modes

**Current repair/measurement status (4 November):**
[baseline status and remaining M10 work](../../docs/poc-baseline-status.md).
The M10 quality/measurement gate is **not complete**; historical numbers below
are not the latest validated comparison.

One talkable CLI exposes three **typed** conversation modes. Realtime and the
optional native separate engine use full Pipecat pipelines; audio and default
separate share the HTTP engine plus Pipecat's VAD utilities.

## Shared thinking model (M11)

All three modes now use **the same task-reasoning model**, configured once:

```ini
[reasoning]
model = openai/gpt-4o-mini
```

`CHAT_THINK_MODEL` overrides this value for every mode. `--model` still changes
only the voice frontend. Realtime now needs **both** API keys.

- **Realtime:** stream input over the realtime connection, use its actual
  `gpt-4o-mini-transcribe` transcript for shared reasoning, then synthesize
  the answer in an isolated realtime response. The original audio question
  is excluded from synthesis so the voice model cannot re-answer it.
- **Audio:** require a `reason` handoff carrying the complete request; the
  shared model answers and owns bash; the audio model reads that answer with
  tools disabled. Direct answers that bypass the handoff are rejected.
- **Separate:** dedicated STT → the same shared reasoning implementation →
  dedicated TTS. The optional native pipeline uses the same model setting,
  reasoning prompt and bash schema through Pipecat's streaming LLM service.

The dummy meeting/calendar tool was removed. **Bash is the only task tool**;
Pi can continue to be invoked through bash, as the user has already verified.
There is no new Pi-specific integration or tool in this change.

All three primary modes passed one six-task functional replay with real API
requests. These are not five-minute comparisons or human quality approval.
Native speech processing is still not independently observable; realtime's
transcription events expose no billable usage here, so total cost stays unknown
rather than silently omitting STT. See the baseline status for the run receipts.

## Interruption and long-running requests

New speech cancels the previous task as well as voice playback. Foreground bash
uses cancellable process groups: cancellation/timeout kills its children and
releases the reasoning lock before handling the next request. Pending tool calls
and their cancellation status are retained in session reports.

The shared reasoner has a default **90-second whole-turn deadline**, configurable
with `CHAT_TURN_TIMEOUT`; individual bash commands remain bounded to 60 seconds.
Realtime speaks a recovery message on reasoning timeout/failure. The optional
native separate streaming adapter has cancellable bash but not this whole-turn
deadline. This CLI is not a background scheduler: indefinite counting should
be replaced with a finite alternative. Cancellation cannot undo completed side
effects, manage detached jobs, or guarantee a provider stops billing an HTTP
request already sent.

Paid functional regression (about 25 seconds per primary mode):

```bash
uv run python baseline.py --interrupt-regression
```

It starts a real sleeping bash task, interrupts with another spoken question,
verifies cancellation, and checks the next answer without restarting. This is
not a five-minute baseline or a human/echo-quality test.

## Modes — `./run.sh <mode>` (no arguments prints the menu)

| Mode | Configured quality baseline | Engine |
|---|---|---|
| `realtime` | `gpt-realtime-2.1-mini` | Pipecat, explicit client-side turn strategy |
| `audio` | full `openai/gpt-audio` (more expensive than mini) | HTTP audio in/out and tool follow-up |
| `separate` | online `gpt-4o-mini-transcribe` → `gpt-4o-mini` → dedicated Coral TTS | Serial HTTP; `--pipecat` selects native streaming |

Models per mode live in `models.ini` (`[realtime] / [audio] / [separate]`);
`--model <name>` overrides the voice/realtime model for one run.

Usage:
```bash
./run.sh realtime | audio | separate   # pick the pipeline type
./run.sh separate --pipecat          # framework-native pipeline (chat_sep.py)
uv run python benchmark.py            # refresh docs/benchmarks/sessions.{csv,json}
./run.sh realtime -d                   # debug (mic levels, VAD, pipecat logs)
./run.sh audio --model openai/gpt-audio
uv run python baseline.py --prepare-only --smoke       # local synthetic fixtures
uv run python baseline.py --mode realtime --smoke      # paid six-task test; no mic
uv run python baseline.py --mode realtime              # paid ~5-minute interactions
```

Keys in git-ignored `.env`: `OPENAI_API_KEY` (realtime/STT/TTS),
`OPENROUTER_API_KEY` (audio/shared inference). Realtime and separate need both. Explicit
environment overrides win over `.env`; model presets come from `models.ini`.

## Tools (M8 scope)

All modes' shared thinking model has one task tool, `bash` — runs a
bash command in the **workdir** and returns stdout/stderr (60 s timeout,
output capped at 4 KB). The workdir is the directory `run.sh` was launched
from, or `--folder <path>`; it is shown in the banner as `[workdir]` and in
the system prompt, so you can ask Friday things like *"summarize the files
in this folder"*.

## Reading the terminal

| Line | Meaning |
|---|---|
| `You: …` (cyan) | what you spoke (local Whisper transcription of the mic capture — the same audio the agent receives) |
| `Friday: …` (green) | the agent's spoken reply |
| `[tool] name(args) -> result` (magenta) | a tool call the agent made |
| `── turn N …` (dim) | operational feedback; the versioned JSON has full timing boundaries and pricing provenance |
| `[warn]` / `[error]` | yellow / red problems |

Mic/VAD chatter, frame counts, and the pipecat banner are suppressed —
run with `-d` to see them. `./run.sh` with no arguments prints the mode
menu; unknown modes are rejected instead of silently falling back.

Companion probes:
- Raw websocket: `../realtime-model-poc/` (the latency/behavior baseline)
- Original cheap-path engine: `../audio-model-poc/` (audio/separate modes
  here are its proven engine, moved in so all three modes share one CLI)

## Setup

Environment is project-level via `uv` (venv at `.venv`, Python 3.11 via
`.python-version`); `.env` is git-ignored and holds `OPENAI_API_KEY`.

```bash
uv run --project experiments/pipecat-poc experiments/pipecat-poc/probe.py
```

## Historical results (2026-10-30, Pipecat 1.15.0, `gpt-realtime-2.1-mini`)

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
./run.sh realtime           # add -d for mic levels, VAD events, logs
./run.sh realtime --model gpt-realtime-2.1   # model override
```

Env overrides: `CHAT_INPUT_DEVICE` (mic index), `CHAT_MIN_VOLUME`
(VAD loudness threshold, default 0.15 — this machine's mic reads a loud
noise floor, so tune if VAD never fires). Headphones recommended:
no echo cancellation, so speaker bleed triggers barge-in.
`CHAT_END_SILENCE` controls realtime pause tolerance (default 0.8 s). The stop
strategy is explicitly silence-based and does not wait for transcripts; changing
this setting trades pause tolerance for endpointing delay.

## Reports and verification

New sessions write atomic, timestamped `session-*.json` reports with schema version
2. They retain model IDs, raw per-stage usage, estimated/provider cost basis,
transcripts, tool results, missing measurements and failures. Native model
inference is bundled and is **not** silently counted as zero or subtracted from
text/audio token buckets. Playback timing is a PortAudio DAC estimate, not a Teams
or acoustic measurement. See the linked baseline status for limits and review steps.

The collator writes `docs/benchmarks/sessions.{csv,json}` and `rejected.json`.
Old reports remain untouched; invalid JSON is surfaced and absent model IDs stay
`unrecorded`. `baseline.py` checks operational/keyword results but never marks
human quality or pricing as verified.

## Timebox

~1 h 20 m used (limit 2 h): ~1 h spent on the two pipecat-1.15 gotchas and
the server-VAD finding, ~20 m on the working probe + docs.