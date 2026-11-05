# Autonomous implementation pass — 5 November 2026

## Scope and release status

The user asked to finish work that can be done independently, timebox blocked
items, and leave a concrete list for joint follow-up. This is an engineering
checkpoint, **not production approval or a claim that everything is finished**.
No private-account changes, worker-service deployment, account resets or pushes
were performed. Raw meeting transcripts and saved session receipts are unchanged.

## Implemented and checked

### Managed background work in the three primary voice approaches

The user's latest pre-change receipt,
`session-realtime-20261105T075014714036Z.json`, records the blanket refusal to
start/track background work. That old prompt policy has been replaced with an
actual managed-job capability, not another claim that bash is unavailable.

- `background_start` accepts bash work immediately and returns a job ID.
  Normal `bash` remains foreground and cancellable. No Pi SDK was added.
- One background job may run at a time. Friday can answer other questions,
  inspect its status/recent output, and explicitly cancel it. New speech stops
  the foreground reply/task, **not** the managed background job.
- Output is a bounded 4,000-byte tail, including while the job is running.
  Twenty recent job records are retained. Job failure, timeout and cancellation
  are distinct states, and the final job records are saved in the session report.
- The default background deadline is 600 seconds, configurable through
  `CHAT_JOB_TIMEOUT` or the launch tool, with a maximum of 3,600 seconds.
  The whole foreground thinking turn remains bounded to 90 seconds.
- Graceful exit, Ctrl+C, SIGTERM and terminal hangup close owned jobs and reap
  their bash process groups. Hard crashes/SIGKILL, detached child sessions,
  restart persistence and multiple writers are **not** managed/sandboxed.
- Completion/failure produces a CLI notice. Friday receives current job states
  on subsequent questions and can speak status on request. Unsolicited spoken
  completion announcements are **not implemented**; they must not interrupt a
  live user conversation.
- The optional native separate adapter also has job tools and shutdown cleanup;
  the live checks below cover the three default approaches, not that adapter.

The final regression specifically requires **numeric counting output**, not just
an arbitrary changing timestamp. In each approach it starts counting, asks an
unrelated arithmetic question while counting continues, reads progress, and
explicitly cancels the same job. All four checks passed in each report:

| Mode | Receipt |
|---|---|
| Realtime | `session-realtime-20261105T090406584075Z.json` |
| Audio | `session-audio-20261105T090519074464Z.json` |
| Separate | `session-separate-20261105T090627468668Z.json` |

Earlier background-mechanics checks also passed, but the model printed timestamps
rather than numbered counts. Those original receipts are retained; the stricter
numeric regression supersedes them for the counting example.

### Automatic model sweep and controlled comparison

`compare_models.py` runs the selected voice configurations sequentially, fixes
the shared thinking model, verifies every observed inference-model ID, retains
failures, and writes an index with cost/latency and transcript diagnostics. It
does not select a human-quality winner or silently substitute failed models.

All four configurations in the first sweep used `openai/gpt-4o-mini` for task
reasoning and the same six-task replay, repeating for approximately five minutes.
The full/mini/separate runs below share that development snapshot; later Pi tool
instructions and the dedicated background-start schema are not retrospectively
included in those measurements.

| Configuration | Measured interaction time | Spoken turns | Mean speech-end → DAC playback | Reported/estimated cost per conversation minute | Operational result |
|---|---:|---:|---:|---:|---|
| Full audio | 308.2 s | 20 | 5.85 s | $0.03564 | 20 keyword checks passed |
| Audio mini | 312.7 s | 16 | 5.52 s | $0.00480 | 3 keyword checks failed; not qualified |
| Separate | 309.1 s | 20 | 4.56 s | $0.00494 | 20 keyword checks passed |
| Realtime, first attempt | 68.5 s, stopped early | 0 | Unknown | Unknown total | No audible response; failed |
| Realtime, bounded recheck | 303.8 s | 21 | 4.30 s | Unknown total | 21 keyword checks passed |

Sources: `comparison-20261105T081806706332Z.json`, including the four linked
session receipts. The single bounded realtime recheck is
`comparison-20261105T090627614352Z.json` →
`session-realtime-20261105T091133604155Z.json`. It used the same thinking-model
ID, with the later background-tool/Pi instructions and corrected cache prices.
Its known instrumented subtotal is about $0.00931 per conversation minute, but
that **excludes missing transcription charges** and is not a full price.
No older short realtime result is substituted into the five-minute comparison.

The first realtime attempt received PCM bytes but no sample crossed the reporting
energy threshold and no spoken transcript completed. The code now tracks PCM
peak amplitude and detects a completed silent response as an explicit failed turn
instead of waiting for the 60-second fixture timeout. A unit test covers that
recovery path. The later pass does not establish the original provider silence
can never recur; both receipts are retained.

Separate's measured serial inference-excluded latency was 3.10 seconds. Full
versus mini differed by only about 0.33 seconds in this workload, **not** evidence
for a two-second end-to-end target. Mini's lower charge does not establish parity.

Provider speech transcripts exactly matched the reasoning answer after ignoring
case/punctuation on 14/20 full-audio turns, 1/16 mini turns, and 20/20 separate
turns; the realtime recheck matched on 19/21 turns. This is a **verbatim-transcript diagnostic**, not a semantic or acoustic
quality score. Mini sometimes included frontend instructions in the purported
user transcript and then spoke about the reason tool instead of the actual task.
Full audio sometimes added an introduction while reading the supplied answer.

Do not change defaults solely from these numbers. Human voice/noise review and
review of the provisional prices remain release gates.

### Pricing evidence and accounting correction

- Public OpenRouter model prices and official OpenAI pricing excerpts were
  saved in `docs/benchmarks/pricing-20261105.json`. No billing/account endpoint
  was accessed.
- Corrected realtime cached-input pricing: audio $0.30/M and text $0.06/M,
  **not** an unrelated universal 50% discount. Normal realtime audio $10/$20
  and text $0.60/$2.40 per million match the retrieved list prices.
- Dataset columns prefixed `repriced_` recompute from retained usage at those
  list rates; the original report totals and receipts remain untouched.
- OpenRouter's returned cost takes precedence. Missing realtime transcription
  usage still leaves the full cost unknown. Native voice/internal inference
  cannot be independently removed as if free. Native realtime usage attribution
  across late events/barge-ins is not fully validated; interrupted-turn figures
  are not audited per-request charges.
- Separate TTS still uses the labelled duration/token-density estimate; text
  input charges and exact output usage are not reconciled. Its numeric cost is
  provisional, not an exact bill. API spend inside Pi/bash is outside the voice
  report's accounting scope and remains unknown, not zero.

### Read-only Friday integration foundation

The shared-reasoner frontends can opt into `worker_context` using
`CHAT_WORKER_SOCKET` and `CHAT_WORKER_REQUESTER`. The bridge calls only the
existing `context.get` operation with a fixed configured identity and
`in_meeting=False`; the model cannot change identity, auto-enrol itself into a
team, or call arbitrary administrative RPCs through this tool. Disconnection and
timeout return visible tool failures rather than hanging the conversation.

A synthetic integration test uses the actual backend context builder, SQLite KB,
and Unix socket protocol. It found and fixed a cross-team active-meeting context
gap: membership in any team could previously expose another team's active
transcript. Meeting filtering now also covers the question and action-context
builders, including explicitly selected meeting IDs.

This is an **opt-in trusted-operator bridge**, not speaker authentication, a full
replacement voice engine inside the deployed worker, or a security sandbox for
bash. The existing worker and its services were not changed in deployment. The
optional native separate adapter does not expose this context bridge.

## Bounded attempts that are parked, not completed

### Spoken Friday → Pi → background game workflow

The user clarified that Friday must delegate game creation/changes to Pi. A
standalone harness that directly invoked Pi was stopped and removed; its failed
component-test receipt remains, but it is **not acceptance evidence**.

`task_acceptance.py` now only supplies synthetic spoken instructions and observes
the result in an isolated browser. It never invokes Pi or writes game code. All
coding is left to Friday's real tool calls and Pi.

Observed outcomes:

1. With the original tool contract, Friday ran Pi in the foreground; the fixture
   timed out. Failed receipt: `voice-task-20261105T084413728349Z.json`.
2. With dedicated background tools, Friday invoked Pi from speech and Pi created
   a game that passed browser click/reset. However, Friday still performed the
   coding step in foreground and only backgrounded the server, contrary to the
   request. The acceptance gate correctly failed. Receipt:
   `voice-task-20261105T084854016271Z.json`.
3. One stronger thinking-model check (`openai/gpt-4.1-mini`) did not solve the
   orchestration issue: it embedded game HTML in an invented Pi `--content`
   invocation rather than correctly delegating coding. The actual CLI rejected
   that flag. Receipt:
   `voice-task-20261105T085245710972Z.json`.

The coding/delegation capability is demonstrated in part, but **reliable
background Pi orchestration, spoken follow-up changes and final link acceptance
are not signed off**. No manual game repair was substituted. This branch was
parked after those bounded attempts; the stronger model was not made the default.
A useful next engineering step is a small, tested Pi CLI invocation contract,
still through bash, rather than another open-ended prompt-tuning loop or an SDK.

### Full worker deployment and Excel Online

- Final voice-engine selection/quality review is still open. Full meeting audio
  routing, wake/permission preservation and live worker regression are not
  implemented/validated by the read-only bridge. Do not treat this as deployment
  complete or merely a missing launch command.
- Excel Online requires a designated authorized test workbook/account and a
  permitted edit range. No personal browser profile was opened or modified.
- Persistent scheduling, restart-resilient jobs, multiple concurrent editing
  jobs, automatic conflict prevention and proactive spoken completion notices
  remain separate engineering work, not features silently included here.
- Echo handling, reconnect/session resumption and the earlier terminal-jank
  report remain unqualified. No 30-minute real-meeting cost sample was run;
  five-minute synthetic sessions do not replace that acceptance gate.

A [joint voice demo checklist](voice-demo-checklist.md) records concrete natural
counting, actual Friday-to-Pi and authorized Excel Online acceptance steps.

## Joint follow-up

1. Review the new background-counting interaction with a natural microphone.
2. Review the full/mini/separate findings and realtime recheck before selecting
   the voice candidate; have Viswanath review the accounting values.
3. Unblock the Pi CLI/task-orchestration contract and retry the actual spoken
   build/change workflow—not a prebuilt game.
4. After the release gates pass, finish worker voice routing with the intended
   identity/permissions, then exercise real meeting actions.
5. Supply a non-sensitive Excel Online test workbook and permitted edits for the
   browser demo. Do not send credentials in chat or commit them.

## Reproduction

From the repository root; voice tests incur API charges:

```bash
.venv/bin/python -m pytest -q
experiments/pipecat-poc/.venv/bin/python -m unittest discover -s tests -p 'test_poc_http.py' -q
experiments/pipecat-poc/.venv/bin/python experiments/pipecat-poc/baseline.py --background-regression
experiments/pipecat-poc/.venv/bin/python experiments/pipecat-poc/compare_models.py
.venv/bin/python experiments/pipecat-poc/task_acceptance.py --mode separate
python3 experiments/pipecat-poc/snapshot_pricing.py
python3 experiments/pipecat-poc/benchmark.py
```

The Pi task test is retained as a **known failing/partial acceptance test**, not
an approved demo. Offline verification: **70 passed, 16 skipped** in the root
interpreter; all **16** HTTP/configuration tests passed in the POC interpreter.
