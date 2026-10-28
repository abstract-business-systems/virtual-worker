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

*The transcript does not state the recording date for M3; 2026-10-28 is the
recording file's added date.*

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

## 4. Journal

Newest entries appear first.

*The recording date for M3 is not stated in the transcript; 2026-10-28 is the
file-added date.*

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
| 7 | Brainstorm the functional acknowledgement interface | M3 | Engineering | Open — summarize what was heard, allow correction, and drop the current process on interruption |
| 8 | Organize existing latency data and define multi-level measurements | M3 | Engineering | Open — record system, intermediate/client, backend, and user-prompt timing before changing code |

## 6. Document map

| Document | Purpose |
|---|---|
| `meetings.md` | Living index, summaries, journal, and action items |
| `recordings/1.vtt`, `recordings/2.vtt`, `recordings/3.vtt` | Immutable meeting source transcripts |
| `requirements.md` | Product requirements from M1 |
| `next-steps.md` | Follow-up requirements and priorities from M2 |
| `latency-findings.md` | Local latency measurements and improvement options |
| `architecture.md` | Current deployed system architecture |
| `design.md` | Current deployed design and implementation notes |
