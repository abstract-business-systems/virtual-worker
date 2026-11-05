# Joint voice acceptance checklist

Current implementation/results: [autonomous checkpoint](autonomous-wrap-up.md).
This is a test plan, **not a statement that the Pi or Excel demo is complete**.
Use a trusted operator session and non-sensitive test data. Do not attach the
unrestricted bash CLI to an untrusted meeting as if it authenticated speakers.

## 1. Background task and conversation

Launch any primary mode in a disposable working folder. Say naturally:

1. “Start a background task that prints the numbers one to one hundred, with a
   one-second pause between numbers. Tell me the job ID.”
2. “While that runs, remember that our project is called Copper Finch.”
3. “What is seven plus five?”
4. “What is the counting job doing now?”
5. “What project name did I give you?”
6. “Stop the counting job.”

Check that the arithmetic and memory answers arrive without stopping counting;
status reflects changing output; only the explicit stop ends the job. Interrupt
Friday's speech once and verify the job still runs. Counting is time-bounded,
not a permanent service. The default deadline is ten minutes, and graceful
session exit also stops managed jobs.

This sequence passed synthetic operational checks; natural-mic/noise acceptance
still needs a person. Completion appears in the CLI; unsolicited spoken
completion notices are not implemented.

## 2. Friday delegates coding to Pi — currently blocked/partial

The goal must flow through Friday, not through a separately prepared game:

1. Ask Friday to use Pi to build a small browser game in the disposable folder,
   run the work in the background, launch it locally and provide its link.
2. Check that a managed job ID is returned **before** coding completes.
3. Ask an unrelated question while Pi is working.
4. Ask for progress and the link once ready. Open the link and play the game.
5. Ask for one small behavior change, delegated to Pi again, then verify it in
   the browser. The game type need not match a fixed example.

Inspect the real tool/job records: Pi must perform the coding; Friday must not
replace that with its own hand-written game or claim completion from a queued
job. Keep servers bound to loopback unless wider access is explicitly authorized.

Known blockers from the bounded autonomous attempts: thinking-model selection of
foreground rather than background execution, and invented Pi CLI flags. One
speech-driven attempt did have Pi create a working click/reset game, but the
full background/change acceptance failed. Fix the invocation/orchestration
contract first; do not repeatedly rerun a long demo hoping for a lucky pass.

## 3. Excel Online — authorized test workbook required

Before starting, identify the authorized worker account, an empty/disposable
workbook or copy, the allowed edit range, and whether the worker may save it.
Do not use a personal production workbook or share credentials in chat.

Example non-sensitive acceptance sequence:

1. Ask Friday to open the designated workbook and create a small task table in
   the permitted range, using invented task/owner names and statuses.
2. Ask it to change one status, add a simple count formula and read the result.
3. Give one follow-up correction and verify the cells/formula directly in
   Excel Online, not just the agent's spoken claim.
4. Confirm save behavior; keep/delete only the designated test copy as agreed.

A local CSV, a spreadsheet clone or a prepared screenshot is **not** a substitute
for the real Excel Online interaction. No Excel Online acceptance has been run.
Screen sharing/recording may help review but is not a mandatory requirement.

## Evidence and stop conditions

Keep the session report, job state, actual task output, browser/cell check and
failures. Distinguish provider transcripts from what a human actually heard.
Stop after a bounded failure and record its cause; do not use manual repairs to
turn an agent failure into a claimed demo pass. Costs inside Pi/bash and real
meeting transport are not fully captured by the voice-only instrumentation.
