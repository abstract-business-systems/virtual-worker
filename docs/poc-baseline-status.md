# POC repair and baseline status — 4 November 2026

**Status: engineering repairs and controlled measurements exist; the M10 gate is
NOT complete.** Human conversation-quality review, bill/rate verification, a
full-length realtime run on the final stop strategy, and native-model inference
separation remain open. No worker-backend integration or model-swap sweep was
started.

## Changes made

- Fixed separate-mode launch with its optional environment variable unset.
  Explicit environment overrides now take precedence over local secrets.
- Fixed HTTP stream tool-call IDs/indices, retained every text-model request's
  usage, handled all parallel calls, and bounded the text-model tool loop.
- Audio tool follow-up uses a plain text result message with tools disabled:
  avoids the provider's rejected `role:tool` round-trip and repeated side effects.
  Empty answers/provider failures are recorded as failures, not successful turns.
- Separate mode now streams dedicated TTS before the whole WAV downloads;
  its input buffer retains pre-roll and VAD stopping frames instead of clipping
  words. Dedicated online STT and Coral TTS are the configured baseline.
- Audio's configured baseline is **full `gpt-audio`, not mini**: quality first,
  per M10. This is more expensive and is not a cost-optimization decision.
- Added schema-v2 reports with actual model IDs, raw stage usage, pricing basis,
  input/output durations, errors, transcripts, tool results and timing boundaries.
  Missing usage or an unknown model rate stays unknown, never free.
- Rebuilt the collator: corrected seconds-to-minutes conversion, stopped guessing
  models from filenames, retained failed modern runs and exposed rejected reports.
  Historical reports and meeting transcripts were left untouched.

## Controlled measurements retained

These are synthetic English utterances replayed through the **existing CLI
engines**, not live Teams or human conversational-quality tests. All pipelines
use the same six tasks: arithmetic, set/recall a code name, list documents,
summarize one document, and call the stub meeting-context tool.

| Path | Sample | Completed responses | Mean stop-speaking → playback | Total cost/minute | Voice-only cost/minute | Inference-excluded voice latency |
|---|---|---:|---:|---:|---:|---:|
| Separate, serial HTTP engine | 308.4 s | 20/20 | 5.09 s | ~$0.004612 | ~$0.004098 | 3.33 s |
| Audio, full `gpt-audio` | 304.9 s | 17/17 | 7.79 s | $0.121898 | Unknown: bundled inference | Unknown: bundled inference |
| Realtime, **final explicit stop strategy; regression only** | 28.3 s | 3/3 | 1.96 s | ~$0.014534 | Unknown: bundled inference | Unknown: bundled inference |

**Do not rank these rows as a verified comparison.** The realtime sample is
shorter and contains only the pause regression, unlike the six-task longer runs.
Completed responses and heuristic keyword checks are not human quality approval.
The full-audio responses also showed contextual verbosity/drift in one summary;
this must be assessed in the live review.

Source reports (relative to `experiments/pipecat-poc/results/`):

- `session-separate-20261104T072826419831Z.json`
- `session-audio-20261104T072317171677Z.json`
- `session-realtime-20261104T074859188510Z.json`

Additional smoke tests: all six tasks completed in realtime, full audio, serial
separate and optional Pipecat-native separate. The latter has raw STT usage and
LLM usage capture plus estimated TTS charges now; its overlapping streaming
inference/TTS cannot be treated as a serial latency subtraction.

### Measurement and pricing limits

- Speech end in fixture runs comes from the last non-silent input sample.
  Live capture uses an input-energy proxy, not a human-annotated speech boundary.
- First playback is the first non-silent PCM sample's **PortAudio DAC estimate**.
  This includes endpointing and local playback buffering, but is not an acoustic
  microphone/loopback measurement, word recognizer or remote Teams delivery.
- Per minute means established-session wall time, including listening, inference,
  playback and normal inter-turn gaps. The harness repeats interactions for about
  five minutes; it does **not** pad the sample with idle time. Different response
  lengths/history growth mean different turn counts. Do not extrapolate to a
  meeting-hour or monthly product scenario.
- OpenRouter's returned cost is retained. OpenAI transcription/realtime token
  rates, cache discounts and TTS seconds-to-token pricing are explicitly
  **provisional assumptions**, not reconciled against a bill. Thus cost totals
  with an approximation are not financially verified.
- Serial separate mode excludes the measured text-inference/tool window. The
  realtime/audio APIs bundle native inference with the voice-model request;
  subtracting audio/text token buckets does NOT isolate task reasoning.
- A separate inference model can in principle be called from a function/tool
  handler, but no shared-inference arrangement has been built and live-verified.
  It must not be reported as done merely because function callbacks exist.

## Realtime investigation and bounded stop point

The initial 0.3-second VAD setting split a single request, “What is seven plus
five? Answer in one short sentence,” at the clause pause. A fixture-only wire
trace showed two audio commits and a cancellation. Transcribing each committed
buffer confirmed that the question and its instruction were sent separately.

Increasing silence tolerance to 0.8 seconds produced a three-repeat pass, but a
subsequent five-minute run **still failed the arithmetic keyword check four
times**. The failed results are retained, not discarded or promoted to a baseline:

- `session-realtime-20261104T071801140582Z.json` — original policy; failed checks,
  and one missing playback timestamp.
- `session-realtime-20261104T074456244824Z.json` — 0.8-second VAD with framework
  default stop strategies; four failed arithmetic checks. **Not an accepted run.**

Inspection then found the framework's default smart-turn stop strategy, which
could wait for a roughly five-second fallback on this direct-audio path. The
final configuration explicitly selects `SpeechTimeoutUserTurnStopStrategy`,
with no transcript wait and no extra timeout beyond the 0.8-second VAD silence.
This removes an uncontrolled default; it does not establish that every remaining
model-quality failure has been solved.

The final short regression returned twelve correctly **3/3**, with **one commit
per request**, and measured 1.96 s mean speech-end to estimated playback. The
full five-minute suite was **not rerun after this final strategy change**. Work
stopped here to avoid another extended testing loop after the user's pause.

## Reproduce

From the POC directory:

```bash
# No API requests: generate synthetic input WAVs and a safe document folder.
uv run python baseline.py --prepare-only --smoke

# Paid API requests: three repetitions of the pause regression (~30 s).
uv run python baseline.py --mode realtime --pause-regression

# Paid API requests: one six-task pass, not the five-minute M10 sample.
uv run python baseline.py --mode realtime --smoke

# Paid API requests: approximately five minutes of repeated interactions.
uv run python baseline.py --mode realtime

# Other fixed baselines; no automatic model-swap sweep.
uv run python baseline.py --mode audio
uv run python baseline.py --mode separate
uv run python baseline.py --mode separate --native-separate --smoke

# Refresh data; bad historical JSON is surfaced in rejected.json.
uv run python benchmark.py
```

`CHAT_END_SILENCE` overrides the realtime pause allowance (default 0.8 s,
validated range 0.1–3.0 s). A longer pause can still end a turn: this is not a
complete semantic conversation-state implementation. Use headphones for live
checks; echo cancellation and reconnect/resume are not solved here.

## Remaining work, in order

1. Run the six-task/five-minute realtime suite with the final explicit stop
   strategy. If it fails, report the failure rather than extending an unbounded
   debugging/model-swap exercise.
2. Demonstrate a shared external inference model alongside realtime/audio and
   establish what can actually be excluded from their voice cost and latency.
   Native internal inference currently remains unobservable.
3. Verify the applicable rates/cache treatment against provider pricing/billing.
4. User tests all three modes with real speech, follow-ups, pauses, corrections,
   document requests and interruption; Viswanath reviews the baseline numbers.
5. Only after this gate: automated model swapping and worker integration.

Offline verification: repository suite **46 passed, 6 skipped** (audio interpreter
needed for those six); all **6** HTTP/configuration tests pass using the POC
interpreter. Python syntax checks pass. No new meeting requirements were invented.
