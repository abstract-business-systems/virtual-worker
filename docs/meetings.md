# Virtual Worker — Meeting Index, Summaries and Journal

**Living document.** Update this file whenever a new meeting transcript is added
to [`recordings/`](recordings/).

- **Source of truth:** raw transcripts in `docs/recordings/*.vtt` are immutable.
- **Detail documents:** [`requirements.md`](requirements.md),
  [`next-steps.md`](next-steps.md), [`latency-findings.md`](latency-findings.md),
  [`architecture.md`](architecture.md), and [`design.md`](design.md).
- **Rule:** decisions come from the transcripts and explicit project decisions;
  do not invent requirements.
- **History rule:** later decisions may supersede earlier ones, but earlier
  decisions are never silently rewritten.
- **Security rule:** never place passwords, API keys, or private account data in
  transcripts, summaries, or project documentation.

## 1. How to maintain this document

When a new recording `N.vtt` is added:

1. Commit the raw transcript under `docs/recordings/` first.
2. Add the next meeting row to the index below.
3. Read the transcript fully and add a concise summary.
4. Add a journal entry at the top of the journal.
5. Refresh the action items and open questions.
6. Fold confirmed decisions into the relevant detail document and link back to
   the meeting.
7. Never edit an existing raw transcript.

## 2. Meeting index

| # | File | Date | Length | Participants | Topic | Detail lives in |
|---|---|---|---|---|---|---|
| M1 | [`1.vtt`](recordings/1.vtt) | 2026-10-15 | 9m | Viswanath Chidambaram; Sarooparam Chenaram Darji | Initial virtual-worker requirements and proof-of-concept scope | [`requirements.md`](requirements.md) |
| M2 | [`2.vtt`](recordings/2.vtt) | 2026-10-16 | 22m | Viswanath Chidambaram; Sarooparam Chenaram Darji; Subramaniyan Periyakaruppan; Friday/Luna test voices | Prototype review, Teams behavior, latency/cost priorities, and next steps | [`next-steps.md`](next-steps.md), [`latency-findings.md`](latency-findings.md) |
| M3 | [`3.vtt`](recordings/3.vtt) | 2026-10-28* | 12m | Viswanath Chidambaram; Sarooparam Chenaram Darji | Latency review: functional acknowledgement, evidence-based investigation, and multi-level measurement | [`next-steps.md`](next-steps.md) |
| M4 | [`4.vtt`](recordings/4.vtt) | 2026-10-29* | 10m | Viswanath Chidambaram; Sarooparam Chenaram Darji | Conversation layer, continuous speech processing, acknowledgement, progress updates, and streaming | [`next-steps.md`](next-steps.md) |
| M5 | [`5.vtt`](recordings/5.vtt) | 2026-10-29* | 21m | Viswanath Chidambaram; Sarooparam Chenaram Darji | Perceived delay as the primary goal; conversation states, background work, interruption, and content separation | [`next-steps.md`](next-steps.md) |
| M6 | [`6.vtt`](recordings/6.vtt) | 2026-10-29* | 9m | Viswanath Chidambaram; Sarooparam Chenaram Darji | Follow-up 1:1: voice-model cost research, conversation-layer plan alignment, and a Pipecat demo experiment | [`next-steps.md`](next-steps.md), [`conversation-layer-plan.md`](conversation-layer-plan.md) |

*The transcripts do not state their recording dates; 2026-10-28 is the M3
recording file's added date, and 2026-10-29 is the M4–M6 file-added date.*

## 3. Meeting summaries

### M1 — Initial requirements (`1.vtt`)

The project is a virtual worker that joins an online meeting, listens quietly,
answers when called, remembers useful discussion, and delivers reminders. The
first proof of concept focuses on one worker in one meeting.

The meeting also defined later capabilities: external event handling, shared
knowledge with access control, multiple worker instances, and interruption
handling. The full requirements are in [`requirements.md`](requirements.md).

### M2 — Prototype review and next steps (`2.vtt`)

The prototype could join Teams, search through the browser, and speak, but its
interaction latency and operating cost had not been measured. The immediate
priority became measurement before broader feature development.

The meeting also covered the worker identity, account setup, calls, messages,
inspectable operation, and keeping the control layer thin. The agreed actions
are in [`next-steps.md`](next-steps.md). The later local latency measurement
and its pipeline analysis are in [`latency-findings.md`](latency-findings.md).

### M3 — Latency review (`3.vtt`)

The meeting discussed two parallel directions for latency:

- A functional response should acknowledge the request before the full answer,
  ideally summarize what Friday understood, and let the user identify missing
  information.
- While that acknowledgement is playing, the application could continue its
  work. If the user interrupts, the current process should be dropped and the
  new request should be heard.
- Technical causes must be investigated rather than assumed. The discussion
  mentioned endpointing, STT, model startup, server ping, the client, and the
  provider as possible contributors, but did not identify one as the cause.
- Existing data should be organized in a spreadsheet or CSV and measured at
  multiple levels, including the system, an intermediate/client layer, the
  backend, and user prompts.
- Provider regions and network routing were raised as hypotheses to research,
  not decisions to change.

The follow-up planning items are recorded in [`next-steps.md`](next-steps.md).

### M4 — Conversation layer and streaming (`4.vtt`)

The meeting separated conversational handling from answer content. Friday
should continuously process incoming speech enough to decide whether a sentence
is complete and whether it should listen, wait, acknowledge, or act. It should
not send every fragment to deep reasoning.

The conversation layer should acknowledge an intended action quickly, start
long-running work in the background, provide progress when appropriate, and
allow the user to interrupt or correct the request. The meeting also discussed
streaming STT and TTS so Friday does not wait for a complete transcription or a
complete first-sentence audio file before responding.

### M5 — Perceived delay and conversation states (`5.vtt`)

M5 clarified that reducing **perceived delay** is the primary goal; reducing
actual technical latency is secondary. The conversation layer should make
Friday appear responsive even when the content task takes seconds or minutes.

- The conversation layer handles interaction, acknowledgement, waiting,
  listening, progress, interruption, and resumption.
- The content layer handles the actual reasoning and task result.
- Candidate states include listening, waiting without processing unrelated
  speech, and acting. Friday should decide which state applies as new audio
  arrives.
- A quick acknowledgement can give the content layer time to work. Long tasks
  may provide proactive progress updates and later resume the conversation.
- If the user interrupts, Friday should stop or reconsider the current task.
- Multi-speaker identification and live translation were recognized as later
  layers; the first design should solve the simpler single-speaker case.

The meetings asked for a conversation-flow diagram and a reviewed design before
implementation. The technical latency research remains secondary to this
conversation-layer design.

### M6 — Follow-up 1:1: voice-model cost and Pipecat demo (`6.vtt`)

The meeting aligned the conversation-layer plan and set the next steps:

- **Voice-model costs were never researched.** Earlier work covered only STT
  and TTS models. The assumption that native voice (speech-to-speech) models
  are costly must be revisited with real numbers — specifically the current
  cost of voice models that support background function calls, at the target
  scale (Friday in meetings ~6 hours/day for roughly 10 people).
- If native voice models are not cost-effective, look for an existing project
  that mimics their behavior using STT + LLM + TTS, and compare the cost
  delta and latency. Prices are expected to fall (assumed at least half in
  six months), so current cost must be known but is not disqualifying.
- **The conversation-layer plan was aligned.** The conversation layer is a
  good idea but must not be the only fix: the technical latency issues are
  fixed alongside it. Viswanath also asked a ranking question — if only one
  thing could be fixed, which, and why, or how to split focus between
  perceived delay and technical latency — which still needs a quantified
  answer.
- **A Pipecat demo was approved.** Pipecat's architecture (streaming STT →
  LLM → TTS) supports local faster-whisper STT, ElevenLabs TTS, and
  OpenAI-compatible endpoints (OpenRouter via base URL), so models can be
  mixed without native voice models. Build a small standalone Pipecat demo —
  nothing else — measure its latency and cost, including the
  acknowledge-before-long-process behavior, and use the results to decide
  whether Friday's final implementation could be built on it.

## 4. Journal

Newest entries appear first.

*The recording dates are not stated in the transcripts; the dates shown above
are the recording file-added dates.*

### 2026-10-29 — M6 follow-up: voice-model cost and Pipecat demo

- Added M6 as the follow-up 1:1 with Viswanath.
- Recorded that voice-model costs were never researched and must be measured
  at target scale before choosing a voice approach.
- Recorded the plan alignment: conversation layer first, technical latency
  fixed alongside it.
- Recorded the approved next step: a minimal Pipecat demo measuring latency,
  cost, and acknowledge-before-long-process behavior, before finalizing the
  implementation choice.

### 2026-10-29 — M4 and M5 conversation-layer direction

- Added M4 and M5 as follow-up meetings.
- Recorded perceived delay as the primary objective and actual latency as a
  secondary engineering concern.
- Separated the conversation layer from the content/reasoning layer.
- Added listening, waiting, acting, progress, interruption, and resumption as
  conversation design topics.
- Deferred further latency research until the conversation flow is reviewed.
- Validated the layer-first direction against the measured baselines: the
  conversation layer stays the primary focus, and streaming STT plus streaming
  TTS move into the same first phase because the acknowledgement itself is
  bounded by endpointing, STT, and TTS startup. The remaining technical
  optimizations (model, region, provider, endpoint tuning) stay deferred. See
  [`next-steps.md`](next-steps.md), "Validation of the layer-first order".
- Researched open-source conversation-layer options. Pipecat is the recommended
  full-framework fit; RealtimeSTT, Silero VAD, and smart-turn are the lighter
  component alternatives. See [`conversation-layer-research.md`](conversation-layer-research.md).
  Note: the framework choice is not finalized — the options and their trade-offs
  are also listed in [`conversation-layer-plan.md`](conversation-layer-plan.md).

### 2026-10-28 — M3 latency review and layered measurements

- Added M3 as the latency follow-up meeting.
- Confirmed that functional acknowledgement and technical investigation should
  be pursued together.
- Recorded the instruction to organize and measure the existing data before
  changing code.

### 2026-10-28 — Expanded latency measurement and research

- The worker's timing trace was expanded to separate endpointing, STT, agent
  handling, backend context work, model first token, TTS, and playback.
- A fresh five-trial local run measured 7.77 seconds median to first audio and
  20.32 seconds median to complete the spoken response.
- Network and OpenRouter routing behavior were researched without changing the
  provider, model, region, or worker behavior.
- Findings and remaining measurement gaps are recorded in
  [`latency-findings.md`](latency-findings.md).

### 2026-10-28 — Initial local latency benchmark recorded

- Five local question-and-answer trials were measured through the worker's
  audio pipeline.
- First response audio measured 7.79 seconds at P50 and 9.81 seconds at P95.
- The pipeline analysis and improvement options were recorded in
  [`latency-findings.md`](latency-findings.md).
- This measurement stops at the virtual microphone; it is not a live Teams
  transport measurement.

### 2026-10-16 — M2 follow-up captured

- The prototype review was recorded as M2.
- Latency and cost became development gates before broader feature work.
- A clean Teams identity, call/message validation, and thin inspectable control
  layer were identified as follow-up work.

### 2026-10-15 — M1 requirements captured

- The initial requirements meeting was recorded as M1.
- The first proof-of-concept scope and deferred capabilities were documented in
  [`requirements.md`](requirements.md).

## 5. Open questions and action items

| # | Item | Source | Owner | Status |
|---|---|---|---|---|
| 1 | Reduce the largest local latency contributors | M2 | Engineering | Open — endpointing, STT, model first token, and TTS need A/B tests |
| 2 | Measure realistic meeting cost | M2 | Engineering | Open — approximately 30-minute session |
| 3 | Provision a clean Teams identity named Friday | M2 | Engineering | Open — credentials stay outside the repository |
| 4 | Validate incoming calls and outgoing messages | M2 | Engineering | Open — authorized test participants only |
| 5 | Measure browser and real Teams delivery latency | M2 | Engineering | Open — current benchmark stops at the virtual microphone |
| 6 | Revisit memory and broader employee workflows | M1/M2 | Product and engineering | Deferred until the latency and cost gates are understood |
| 7 | Define the conversation-layer flow and acknowledgement behavior | M3/M4/M5 | Product and engineering | Open — prepare and review a diagram before implementation; first phase also includes streaming STT and streaming TTS (see "Validation of the layer-first order" in [`next-steps.md`](next-steps.md)) |
| 8 | Organize existing latency data and define multi-level measurements | M3 | Engineering | Complete — local and live worker-path findings are recorded in [`latency-findings.md`](latency-findings.md) |
| 9 | Define listening, waiting, acting, progress, interruption, and resumption states | M4/M5 | Product and engineering | Open — keep conversation handling separate from content reasoning |
| 10 | Defer multi-speaker identification and live translation until the single-speaker flow is clear | M5 | Product and engineering | Deferred — add as a later conversation layer |
| 11 | Research current native voice-model costs (supporting background function calls) at target scale (~6 h/day, ~10 people), and compare against an STT + LLM + TTS pipeline that mimics voice models, including cost delta and latency | M6 | Engineering | Open — revisit the earlier "costly" assumption with real numbers |
| 12 | Build a minimal standalone Pipecat demo and measure latency, cost, and perceived delay, including acknowledge-before-long-process behavior | M6 | Engineering | Open — result feeds the implementation-option choice |
| 13 | Answer the M6 ranking question: perceived delay vs. technical latency — which single focus, or what split, and why | M6 | Product and engineering | Open — needs a quantified ranking, not prose |

## 6. Document map

| Document | Purpose |
|---|---|
| `meetings.md` | Living index, summaries, journal, and action items |
| `recordings/1.vtt`, `recordings/2.vtt`, `recordings/3.vtt`, `recordings/4.vtt`, `recordings/5.vtt`, `recordings/6.vtt` | Immutable meeting source transcripts |
| `requirements.md` | Product requirements from M1 |
| `next-steps.md` | Follow-up requirements and priorities from M2 |
| `latency-findings.md` | Local latency measurements and improvement options |
| `conversation-layer-research.md` | Open-source conversation-layer options and recommendation |
| `conversation-layer-plan.md` | Conversation-layer architecture, options (not finalized), and plan for review |
| `architecture.md` | Current deployed system architecture |
| `design.md` | Current deployed design and implementation notes |
