# Voice latency results (5–6 Nov 2026)

"First word" = you finish speaking → the agent's first spoken word reaches you.

## TL;DR

Reviewed by Viswanath (M13, 6 Nov): stage numbers accepted; **full-reply
latency does not matter — first word only**; the realtime 7.5 s spike is
accepted as a one-off.

**All three approaches now have both numbers (CLI / Teams first word) and
costs:**

| Approach | First word CLI → Teams | Cost per listening-minute |
|---|---|---:|
| Separate — ElevenLabs + Luna (today's Friday) | 3.84 → **4.35 s** | **≈₹0.09** |
| Realtime | 1.13 → 10.87 s | ≈₹0.41 |
| Audio (gpt-audio full) | 3.28 → 6.62 s | ≈₹2.16 |

- **Friday's build is the fastest qualified build in Teams and the cheapest**
  (≈₹0.65 per 7-minute session; monthly ≈₹800–1,200 per `money-per-minute.md`).
- **The audio approach failed quality** — a Norwegian reply, spoken hand-off
  meta-text, a tool-loop failure — besides being slower and ~24× costlier.
- **The Teams platform is not the problem:** the recorded echo test measured
  **0.37 s round trip** (ITU-class, normal for Teams/Meet/Zoom), and Friday's
  overhead on plain conversational turns is ~0 (3.86 s live vs 3.84 s CLI;
  the earlier "+0.5 s" mean included bash-tool turns).
- **Where the time goes** (plain Teams turns): silence wait 0.66 s →
  speech-to-text 1.65 s → thinking 2.34 s → first word ~0.9 s into the voice
  call. **Stage durations are not additive** — the voice column is the full
  synthesis call; first audio arrives inside it.
- **Budget check done** (`platform-latency-research.md`): the ~1 s
  conversational-gap target minus the ~0.4 s platform round trip leaves
  ~0.6 s for the pipeline — currently ~3.7 s.
- **Remaining work**: the perceived-delay mechanism (#42 — instant
  acknowledgement, watcher answers "are you working?", ETAs, proactive
  call-back) and automatic model-swap capability (#37); both feed the ~1 s
  conversational-gap target (#38).

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

| # | Configuration | Env | Pause wait | Speech-to-text | Thinking | Voice | First word | Avg over 20 q | Cost (measured) |
|---|---|---|---:|---|---|---|---:|---:|---|
| 1 | Original worker | CLI | 2.01 s | Whisper (local)<br>1.54 s | gpt-4o-mini<br>1.93 s | local<br>2.45 s | 7.77 s | 20.32 s (whole reply, not first word) | no bill recorded (local Whisper + local TTS unbilled); calculated estimate ≈ ₹0.05/turn or less (gpt-4o-mini text only) |
| 2 | Realtime | CLI + Teams | one connection does all three | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | CLI 1.13 s; Teams 10.87 s | CLI 4.30 s; Teams not separable | CLI bench $0.0471 ≈ ₹3.91 (21 turns, ≈₹0.19/turn); Teams live $0.0237 ≈ ₹1.99 (6 turns, ≈₹0.33/turn, ≈₹0.41/min) |
| 3 | One audio model, full | CLI | 0.7 s | gpt-audio<br>0 s, inside same call | gpt-audio<br>inside same call | gpt-audio<br>2.6 s | 3.28 s | 5.85 s | bench $0.1831 ≈ ₹15.20 (20 turns, ≈₹0.77/turn) |
| 4 | One audio model, small | CLI | 0.7 s | gpt-audio-mini<br>0 s, inside same call | gpt-audio-mini<br>inside same call | gpt-audio-mini<br>2.6 s | 3.28 s | none | bench $0.0250 ≈ ₹2.08 (16 turns, ≈₹0.13/turn) |
| 5 | Three calls, OpenAI direct | CLI | 0.5 s | gpt-4o-mini-transcribe<br>1.59 s | gpt-4o-mini<br>2.12 s | gpt-4o-mini-tts<br>1.0 s | 4.74 s | 4.56 s | bench $0.0254 ≈ ₹2.11 (20 turns, ≈₹0.11/turn) |
| 6 | Three calls, OpenRouter voice | Teams | 0.74 s | gpt-audio<br>1.50 s | gpt-6-luna<br>1.98 s | gpt-audio<br>2.77 s | **6.62 s** (4.1–10.1) | none | $0.1469 ≈ ₹12.34 (10 turns, ≈₹1.23/turn, ≈₹2.16/min) |

Row 6 live source: 6 Nov, 10 completed turns (of 22 interactions; 10
interrupted, 2 failed) over 5.7 listening-minutes in a real Teams meeting.
Cost $0.1469 ≈ ₹12.34 for the session (≈ ₹2.16 per listening-minute; ≈ ₹1.23 per completed turn).
Quality problems recorded honestly: one reply came out in Norwegian, three
replies spoke the hand-off meta-text aloud ("Here's the spoken version:"),
one server-start request died in the 4-request tool loop, and
`quality_verified: false` in the report. First word is computed from
first-playback minus speech-end (the report's own voice-latency field was
empty for this run).
| 7 | Three calls, ElevenLabs (today's Friday) | CLI + Teams | 0.89 s CLI · 0.67 s Teams | elevenlabs/scribe-v2<br>CLI 1.67 s · Teams 1.77 s | gpt-6-luna<br>CLI 2.82 s · Teams 2.44 s | elevenlabs/eleven-v4-turbo<br>CLI 1.15 s · Teams 1.85 s | **CLI 3.84 s · Teams 4.35 s** | CLI 7.65 s · Teams 8.11 s | CLI $0.0082 ≈ ₹0.68 (17 turns, ≈₹0.04/turn); Teams $0.0077 ≈ ₹0.65 (13 turns, ≈₹0.05/turn) |

Cost column source: the billed session reports (result JSONs) for each row's
bench/live run; FX ₹84 per $1; per-turn = bill ÷ completed turns. Where a run
produced no bill (row 1's local Whisper/TTS), the cell says so and gives a
calculated text-only estimate instead.

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