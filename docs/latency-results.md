# Voice latency results (5–6 Nov 2026)

"First word" = you finish speaking → the agent's first spoken word reaches you.

## TL;DR

Reviewed by Viswanath (M13, 6 Nov): stage numbers accepted; **full-reply
latency does not matter — first word only**; the realtime 7.5 s spike is
accepted as a one-off. Still owed: the audio approach's Teams number, the
Teams echo experiment, the platform-latency budget check, and the
perceived-delay mechanism (acknowledgement, task watcher, proactive
call-back) — see `next-steps.md` M13 section.

- **Teams overhead on plain conversational turns is ~0** (3.86 s live vs
  3.84 s CLI first word); the earlier "+0.5 s" mean included turns where
  Friday ran bash tools — tool work, not meeting overhead.
- **Where the time goes** (plain Teams turns): silence wait 0.66 s →
  speech-to-text 1.65 s → thinking 2.34 s → first word ~0.9 s into the voice
  call. **Stage durations are not additive** — the voice column is the full
  synthesis call; first audio arrives inside it.
- **Best live build: ElevenLabs + Luna** — 4.35 s first word overall
  (3.86 s on plain turns), 8.11 s full reply, ≈₹0.65 per 7-minute session.
- **Audio approach in Teams: 6.62 s first word, ≈₹2.16 per minute** — slower
  than the ElevenLabs build (4.35 s) and ~6× costlier, with quality problems
  (a Norwegian reply, spoken meta-text, a tool-loop failure). The separate
  approach stays the qualified build; the 3-approach × 2-measure matrix is
  now complete.
- **Realtime caveat**: CLI-fast (1.13 s) but one live session hit a 7.5 s
  in-stream transcription spike; root cause open.
- **Open items**: automatic model-swap capability (#37); perceived-delay work
  toward the ~1 s conversational gap (#38).

## What was asked (M12) → status

| Ask | Answer |
|---|---|
| Two numbers: without Friday, and with Friday in Teams | Measured for two builds — see the table below |
| Stage-by-stage measurement, one step at a time | Every configuration broken into silence wait → speech-to-text → thinking → voice |
| Friday's own overhead | ~+0.5 s in Teams — negligible, as assumed in the meeting |
| Swappable speech-to-text, speed over intelligence | Scribe, OpenAI transcribe and local Whisper all probed from the same host: 1.1–2.0 s |
| Model variants with latency and cost, quality permitting | Four configurations compared; one qualified live build recommended (row 7) |
| Where to fix | The Teams meeting is not the problem (~0.5 s). The model pipeline is: thinking (2.4–2.8 s) and speech-to-text (1.7 s) dominate. One open item: realtime's in-stream transcription spiked to 7.5 s once; root cause not yet pinned |
| Still open | Automatic model-swap capability (#37); perceived-delay work toward the ~1 s conversational gap (#38) — deferred by agreement |

| # | Configuration | Env | Pause wait | Speech-to-text | Thinking | Voice | First word | Avg over 20 q |
|---|---|---|---:|---|---|---|---:|---:|
| 1 | Original worker | CLI | 2.01 s | Whisper (local)<br>1.54 s | gpt-4o-mini<br>1.93 s | local<br>2.45 s | 7.77 s | 20.32 s (whole reply, not first word) |
| 2 | Realtime | CLI + Teams | one connection does all three | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | CLI 1.13 s; Teams 10.87 s | CLI 4.30 s; Teams not separable |
| 3 | One audio model, full | CLI | 0.7 s | gpt-audio<br>0 s, inside same call | gpt-audio<br>inside same call | gpt-audio<br>2.6 s | 3.28 s | 5.85 s |
| 4 | One audio model, small | CLI | 0.7 s | gpt-audio-mini<br>0 s, inside same call | gpt-audio-mini<br>inside same call | gpt-audio-mini<br>2.6 s | 3.28 s | none |
| 5 | Three calls, OpenAI direct | CLI | 0.5 s | gpt-4o-mini-transcribe<br>1.59 s | gpt-4o-mini<br>2.12 s | gpt-4o-mini-tts<br>1.0 s | 4.74 s | 4.56 s |
| 6 | Three calls, OpenRouter voice | Teams | 0.74 s | gpt-audio<br>1.50 s | gpt-6-luna<br>1.98 s | gpt-audio<br>2.77 s | **6.62 s** (4.1–10.1) | none |

Row 6 live source: 6 Nov, 10 completed turns (of 22 interactions; 10
interrupted, 2 failed) over 5.7 listening-minutes in a real Teams meeting.
Cost $0.1469 ≈ ₹12.19 for the session (≈ ₹2.16 per listening-minute).
Quality problems recorded honestly: one reply came out in Norwegian, three
replies spoke the hand-off meta-text aloud ("Here's the spoken version:"),
one server-start request died in the 4-request tool loop, and
`quality_verified: false` in the report. First word is computed from
first-playback minus speech-end (the report's own voice-latency field was
empty for this run).
| 7 | Three calls, ElevenLabs (today's Friday) | CLI + Teams | 0.89 s CLI · 0.67 s Teams | elevenlabs/scribe-v2<br>CLI 1.67 s · Teams 1.77 s | gpt-6-luna<br>CLI 2.82 s · Teams 2.44 s | elevenlabs/eleven-v4-turbo<br>CLI 1.15 s · Teams 1.85 s | **CLI 3.84 s · Teams 4.35 s** | CLI 7.65 s · Teams 8.11 s |

Row 2 live source: 5 Nov, 6 turns over 4.9 min in a real Teams meeting, zero
errors, transcript heard correctly 6/6, bash tools worked live
(`session-realtime-20261105T153546881500Z.json`). Stage means: wait for
silence 0.56 s → transcription 7.54 s → thinking 1.29 s → first word. Cost
$0.0237 ≈ ₹1.99 for the session (~₹0.41 per listening-minute). The 6.6 s gap
vs the CLI bench is almost entirely the transcription call from the abs-worker
host (7.54 s there vs ~1.5 s from the dev machine) — not the Teams meeting
itself, whose transport and silence wait together cost well under 1.5 s.
Probe follow-up (6 Nov, same host, same keys):
`stt-latency-probe-20261106T064127.json` — Scribe 1.71 s, gpt-4o-mini-
transcribe 1.41 s, local Whisper 1.45 s. No persistent host or network
problem; the 7.54 s live stage was transient. Remaining suspects: provider
queueing at that moment, or the in-stream transcription inside the realtime
session behaving differently from standalone calls. Discriminator planned:
re-run the probe during a live session.

Row 7 bench source: 5 Nov, 17 turns over 301 s, all fixture checks passed
(`session-separate-20261105T152427745844Z.json`). Thinking stage average
includes turns that ran bash tools. The thinking model (2.82 s) is now the
biggest single chunk of the first-word delay; the ElevenLabs voice is the
smallest (1.15 s).

Row 7 live source: 6 Nov, 23 turns over 7.7 min in a real Teams meeting
(`session-separate-20261106T065509936538Z.json`): 13 completed, 7 interrupted
by overlapping speech, 1 failed (Friday attempted an internet search with no
web tool and hit the tool-loop limit — a tooling gap, not a latency one).
First word mean 4.35 s (range 2.9–6.6), round trip 8.11 s, cost $0.0077 for
the session. Hearing was good: Friday found and explained the Snake project,
counted its files, and flagged audio cut-outs honestly. **Teams overhead vs
the CLI bench is only ~+0.5 s on the first word** — the estimate is now a
measurement.

Mid-session probe (`stt-latency-probe-20261106T064751.json`, run while the
meeting was live): Scribe 1.71 s, gpt-4o-mini-transcribe 1.12 s, Whisper
2.04 s — STT stayed fast under meeting load, ruling out host load and
provider queueing as the cause of realtime's 7.54 s event. The remaining
suspect is the in-stream transcription inside the realtime session itself.

Voice-stage note: the measured path streams raw PCM straight from OpenRouter's
ElevenLabs endpoint (confirmed `audio/pcm` 5 Nov) — no ffmpeg in it. ffmpeg
decode exists only as a fallback for endpoints that reject PCM and adds
~0.2 s decode plus a full-download wait when it triggers; it was in the path
briefly in the first live build before the PCM switch.

- Env "CLI" = command line with a recorded test voice, no Teams call.
- Env "Teams" = inside a real Teams meeting, which adds ~0.5–1 s of travel.
- "none" in the Avg column = that configuration was never run through a full 20-question session.
- Same model in all three stage columns = one model that listens, thinks, and speaks by itself.

Gap: rows 1, 3–5 were never tested in Teams, row 6 never at the CLI; **rows 2
and 7 are now measured both ways**. Verdict across both pairs: Teams adds
only ~0.5 s to the first word; the latency budget is decided by the model
pipeline, and row 7 (ElevenLabs + Luna) is the best measured live build at
4.35 s first word, 8.11 s round trip.

Bench note: the repeat-suppression meant for live meetings misfires on the
fixture plan (it repeats similar questions on purpose); it was disabled for
bench runs only.

Not committed.