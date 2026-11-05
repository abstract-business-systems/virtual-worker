# POC repair and baseline status — 4 November 2026

**Status: repairs and M11's common task-reasoning model are implemented; the
full measurement/quality gate is NOT complete.** Each primary mode has passed
one six-task replay through the common model. Human quality review, a fresh
longer comparison, native speech-processing costs and bill verification remain
open. No model-swap sweep or worker-backend implementation was started.

## Latest stability fix — 5 November: interrupting long-running work

The user's live realtime report (`session-realtime-20261105T055127416950Z.json`)
answered four requests, then stopped answering after an indefinite-counting
request. Later speech was transcribed but never started reasoning. A background
reasoning task retained the conversation lock; the realtime interruption path
cancelled voice output but not that task. The exact unfinished bash command was
not recorded, so it cannot be inferred from that report.

Checkpoint `d4ad8f5` preserves the pre-fix implementation and user reports.
The fix cancels background reasoning on both interruption and new speech,
awaits cleanup, and releases the lock. Bash now uses a cancellable subprocess
and kills/reaps its process group on cancellation or its 60-second timeout.
Pending tools are recorded before execution, including final cancellation status.
The default shared-reasoner turn deadline is 90 seconds (`CHAT_TURN_TIMEOUT`);
realtime prepares a spoken recovery message on timeout/failure. The optional
native separate adapter also uses cancellable bash, but its streaming service
does not use the shared reasoner's whole-turn deadline.

The reasoning prompt now explains that this request/response CLI is not an
indefinite-counting/background scheduler and should offer a finite alternative.
This is not background-task orchestration. Existing side effects cannot be
undone, detached jobs are not managed, and a provider HTTP request already sent
may still complete/be billed even though the local waiter is cancelled.

A new real-provider regression starts a finite 30-second bash sleep, feeds a
second spoken question while it is running, verifies tool cancellation and
checks the spoken arithmetic answer **in the same process without a restart**:

| Primary mode | Report | Result |
|---|---|---|
| Realtime | `session-realtime-20261105T063758071818Z.json` | Tool cancelled; next answer correct |
| Audio | `session-audio-20261105T063824311340Z.json` | Tool cancelled; next answer correct |
| Separate | `session-separate-20261105T063847948763Z.json` | Tool cancelled; next answer correct |

The replay is approximately 25 seconds per mode, not a five-minute performance
baseline. Offline regressions also exercise real bash plus child-process death,
cancellation during spawn, lock release, provider-waiter cancellation, deadlines,
and both realtime cancellation hooks. The optional native separate adapter was
not live-retested. Natural-microphone/echo quality still needs user review.

Reproduce from the POC directory (paid API calls):

```bash
uv run python baseline.py --interrupt-regression
```

## Common task reasoning (M11)

All modes resolve `[reasoning] model = openai/gpt-4o-mini` from `models.ini`,
with a single global `CHAT_THINK_MODEL` override. Default HTTP audio/separate
and realtime share `SharedReasoner`; the optional Pipecat-native separate path
uses the same model setting, prompt and bash schema through its streaming LLM
service. Task memory and bash decisions belong to the thinking model.

The dummy `get_meeting_context` tool was removed; it returned invented fixed
calendar data, not a real lookup. The final fixture now asks a follow-up about
an actual supplied document. Historical reports are unchanged. The user also
confirmed Pi works through bash: preserve that workflow rather than build a
new SDK/tool integration. A game-launch demonstration is not claimed complete.

Native realtime function arguments proved unreliable as transcripts, and its
voice response could re-answer a request instead of reading the external
answer. Realtime now uses actual input transcription and an isolated synthesis
response containing only the shared model's answer. Automatic responses before
reasoning completes are suppressed. Speech recognition/voice processing still
runs on native services; this does not eliminate their internal inference.

Successful real-provider functional receipts, relative to the results folder:

| Mode | Report | Completed / keyword checks |
|---|---|---|
| Realtime | `session-realtime-20261104T134217389391Z.json` | 6/6, all matched; spoken transcript matched reasoning answer on all six |
| Audio | `session-audio-20261104T132020682243Z.json` | 6/6, all matched |
| Separate | `session-separate-20261104T132154978512Z.json` | 6/6, all matched |

Every inference stage in these reports records `openai/gpt-4o-mini`. All paths
performed real bash calls and retained the code-name memory. These are synthetic
functional checks, not verified conversational quality or latency/cost baselines.
Failed intermediate realtime attempts are retained too, not accepted as baselines.
The optional native separate adapter has configuration/offline coverage but was
not live-retested in this change.

Realtime transcription events do not expose billable usage in this adapter.
Their STT stage therefore has unknown cost, making the total unknown; external
reasoning usage/cost is retained independently. Native voice-only isolation and
pricing verification are still open. The measurements below precede this shared
reasoning implementation and must not be presented as current measurements.

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
| Realtime, **pre-shared-reasoning stop-strategy regression** | 28.3 s | 3/3 | 1.96 s | ~$0.014534 | Unknown: bundled inference | Unknown: bundled inference |

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
- Common external task reasoning is now implemented and functionally checked
  (see latest receipts above). This does not make native speech models' own
  internal processing independently measurable.

## Earlier realtime investigation and bounded stop point (before M11 implementation)

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

1. Compare full audio with mini, as requested by M11; shared reasoning stays
   fixed. Do not claim mini quality or a cost/latency improvement in advance.
2. Run a fresh longer comparison of the selected three candidates using the
   shared model. Resolve realtime transcription usage/pricing and disclose any
   native processing that cannot be separated.
3. Verify the applicable rates/cache treatment against provider pricing/billing.
4. User tests the updated paths with natural speech; Viswanath reviews the
   comparison. Controlled keyword checks are not a replacement for that review.
5. Follow the M11 integration/demo roadmap. Pi-through-bash already works per
   the user; check its task-level acceptance rather than assume a new SDK is needed.

Offline verification: repository suite **58 passed, 15 skipped** (audio interpreter
needed for those fifteen); all **15** HTTP/configuration tests pass using the POC
interpreter. Python syntax checks pass. No new meeting requirements were invented.
