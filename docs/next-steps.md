# Virtual Worker — Next Steps and Engineering Requirements

Source: [`docs/recordings/2.vtt`](recordings/2.vtt)
Additional source: [`docs/recordings/3.vtt`](recordings/3.vtt)

This document captures the priorities, decisions, and follow-up work from the
16 October meeting. It complements the product requirements in
[`requirements.md`](requirements.md); it does not replace them.

## Meeting outcome

The current prototype is moving in the right direction: it can join a Teams
meeting, use the browser to search, and speak in a way that does not sound
entirely scripted. The immediate priority is not to improve memory or perfect
the assistant's answers.

The next development gate is:

1. Measure and reduce interaction latency.
2. Measure the cost of a realistic meeting session.
3. Only then continue with broader assistant behavior and memory work.

Until those measurements are available, the implementation should remain a
thin, inspectable interface around the existing capabilities.

## M3 latency checkpoint — complete

The measurement and research requested by M3 are complete for the current
latency decision. The findings are recorded in
[`latency-findings.md`](latency-findings.md).

The evidence shows that the main delay is inside Friday's processing path:
endpointing and STT, model first-token time, and first TTS audio. Backend
context lookup, basic network connectivity, and the meeting path are not the
main source of the multi-second delay.

No provider, model, region, endpoint, TTS, or agent behavior was changed as
part of this checkpoint. The exact moment a remote participant hears audio was
not separately timed, but that gap is not needed to identify the main cause.

## Remaining next step from M3

Choose which possible response to evaluate first:

1. Reduce actual response time, starting with endpointing and STT.
2. Reduce perceived waiting with an immediate acknowledgement.
3. Evaluate both, with one chosen as the first controlled comparison.

The possible fixes and their order are already documented in
[`latency-findings.md`](latency-findings.md). No further measurement or
research should be added until this choice is made.

## Priority requirements

### 1. Measure end-to-end latency — current checkpoint complete

The local and live worker-path baselines have been measured. They record speech
capture and STT, model first-token time, TTS startup, first response audio, and
full response playback. The results are documented in
[`latency-findings.md`](latency-findings.md).

A specific latency target and cost target still need to be agreed before any
latency or broader feature work is considered complete.

### 2. Measure meeting cost

The system must measure the cost of running the worker in a normal meeting,
independently of whether the answer is correct.

The first cost test should:

1. keep the worker in a meeting for approximately 30 minutes;
2. allow participants to ask it questions and exercise the normal interaction
   path;
3. record model, speech, and other service usage;
4. calculate the cost for the session and, where possible, the cost per
   interaction and per meeting hour.

A spending budget or acceptable cost target still needs to be agreed. Cost
results must be available before committing to further model or feature work.

### 3. Keep the control layer thin and inspectable

The worker's control layer should be small. It should expose the computer,
meeting, account, and available actions to the assistant instead of encoding a
large collection of special-case behaviors in the agent.

Requirements:

- People must be able to inspect what the worker is doing while it operates.
- The worker should observe the current machine and meeting state and act from
  that information.
- Temporary instructions may be used to speed up development, but they should
  not become permanent hard-coded behavior without an explicit requirement.
- Examples of behavior must remain examples, not an unreviewed set of rigid
  rules.
- The system should keep memory and policy information separate from the thin
  interaction layer.

### 4. Operate as a normal company employee

The worker should be configured as a company employee with its own computer,
accounts, usernames, and permitted login steps. It should respond to events
that occur on that computer rather than being built only for one scripted demo.

Examples of expected event handling include:

- answering an incoming call when configured to do so;
- replying to a message when asked;
- carrying out an instruction given by a meeting participant;
- joining a meeting when it receives a valid invitation or instruction.

These examples must be implemented through the worker's general capabilities
and applicable company policy. They must not be treated as permission to take
arbitrary actions.

### 5. Provision a clean Teams identity

The prototype must use a dedicated Teams identity that can be safely operated
by the worker.

The setup must provide:

- a confirmed Teams account for the worker;
- a stable display name, currently **Friday**;
- the required account and computer access;
- credentials provisioned through the project's secure configuration process,
  never committed to the repository or placed in this document;
- a decision on whether existing chat history is retained, archived, or
  replaced with a new account/history.

The worker must not accidentally inherit or expose another person's private
conversation history. Resetting or replacing an account is preferred if its
existing history or profile makes the test ambiguous.

### 6. Validate calls and messages as real interactions

After the clean account is available, the team must test the worker in the
Teams environment rather than only changing its prompt or display name.

The test should verify that it can:

1. remain available in the Teams context;
2. receive and answer an incoming call when instructed;
3. send a message to a specified person when asked;
4. identify itself as Friday when communicating;
5. avoid responding to events or people outside its authorization.

The test should capture failures such as missed calls, excessive response
delay, repeated responses, or responses that merely repeat a generic prompt.

## Ordered implementation plan

### Phase 1 — Benchmark the current prototype

- Add timestamped instrumentation to the interaction pipeline.
- Run a controlled meeting session with several questions and commands.
- Produce a latency report with per-stage and end-to-end timings.
- Run an approximately 30-minute session and produce a cost report.
- Record the models, speech services, configuration, and workload used for
  both reports so the results are reproducible.

### Phase 2 — Remove the largest latency and cost problems

- Use the latency report to identify the slowest stages.
- Propose one or more concrete changes to reduce round-trip time.
- Compare the cost of the current model/service composition with alternatives.
- Re-run the same benchmark after each material change.
- Do not expand the feature set until the team has reviewed the results and
  agreed on acceptable latency and cost.

### Phase 3 — Prepare and test the worker identity

- Create or reset the dedicated Teams account.
- Set the display name to Friday and remove unrelated profile/history state.
- Provision access using the secure deployment configuration.
- Confirm that the worker can join the target meeting and stay available.
- Test incoming calls and outgoing messages with non-sensitive test content.

### Phase 4 — Continue product development

Once the latency and cost gates pass:

- improve the worker's ability to respond to calls, messages, and meeting
  instructions;
- define how policy documents and current machine information are combined;
- revisit memory correctness and meeting-history behavior;
- decide whether old chat history should be archived or discarded;
- expand the worker's event handling only after each action has an explicit
  authorization and test case.

## Explicitly deferred for now

The following topics were discussed but are not the immediate priority:

- making the worker's memory and answers fully correct;
- optimizing repeated speech or other conversational bugs;
- adding a large set of hard-coded action rules;
- deciding whether the current account's chat history should be deleted or
  archived;
- expanding the worker beyond the current Teams-based test;
- adding more advanced employee workflows before the latency and cost
  measurements are complete.

## Acceptance criteria for the next checkpoint

The next checkpoint is complete when:

- a reproducible latency report exists for a realistic meeting interaction;
- a reproducible cost report exists for an approximately 30-minute meeting;
- the largest latency and cost drivers have an agreed improvement plan;
- the worker can use a clean, dedicated Teams identity named Friday;
- a human can inspect the worker's current state and actions while it runs;
- call handling and message sending have been tested with authorized test
  participants;
- no credentials or private account history are stored in the repository;
- the team has explicitly agreed on the latency target, cost budget, and
  account-history policy before broader feature development resumes.

## Open decisions

- What end-to-end latency is acceptable for a spoken interaction?
- What meeting-hour and per-interaction cost is acceptable?
- Which model/service composition should be used after benchmarking?
- Should the existing Teams account be reset, archived, or replaced?
- Which calls, messages, and computer events may Friday handle without an
  additional confirmation?
- Which policy document is authoritative when a user instruction conflicts
  with a configured policy?
- What level of live inspection, logging, and audit history is required?
