# Voice latency results (5 Nov 2026)

"First word" = you finish speaking → the agent's first spoken word reaches you.

| # | Configuration | Env | Pause wait | Speech-to-text | Thinking | Voice | First word | Avg over 20 q |
|---|---|---|---:|---|---|---|---:|---:|
| 1 | Original worker | CLI | 2.01 s | Whisper (local)<br>1.54 s | gpt-4o-mini<br>1.93 s | local<br>2.45 s | 7.77 s | 20.32 s (whole reply, not first word) |
| 2 | Realtime | CLI + Teams | one connection does all three | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | CLI 1.13 s; Teams 10.87 s | CLI 4.30 s; Teams not separable |

Row 2 live source: 5 Nov, 6 turns over 4.9 min in a real Teams meeting, zero
errors, transcript heard correctly 6/6, bash tools worked live
(`session-realtime-20261105T153546881500Z.json`). Stage means: wait for
silence 0.56 s → transcription 7.54 s → thinking 1.29 s → first word. Cost
$0.0237 ≈ ₹1.99 for the session (~₹0.41 per listening-minute). The 6.6 s gap
vs the CLI bench is almost entirely the transcription call from the abs-worker
host (7.54 s there vs ~1.5 s from the dev machine) — not the Teams meeting
itself, whose transport and silence wait together cost well under 1.5 s.
| 3 | One audio model, full | CLI | 0.7 s | gpt-audio<br>0 s, inside same call | gpt-audio<br>inside same call | gpt-audio<br>2.6 s | 3.28 s | 5.85 s |
| 4 | One audio model, small | CLI | 0.7 s | gpt-audio-mini<br>0 s, inside same call | gpt-audio-mini<br>inside same call | gpt-audio-mini<br>2.6 s | 3.28 s | none |
| 5 | Three calls, OpenAI direct | CLI | 0.5 s | gpt-4o-mini-transcribe<br>1.59 s | gpt-4o-mini<br>2.12 s | gpt-4o-mini-tts<br>1.0 s | 4.74 s | 4.56 s |
| 6 | Three calls, OpenRouter voice | Teams | never measured | gpt-audio<br>never measured | gpt-6-luna<br>never measured | gpt-audio<br>never measured | not measured | none |
| 7 | Three calls, ElevenLabs (today's Friday) | CLI + Teams | 0.89 s | elevenlabs/scribe-v2<br>1.67 s | gpt-6-luna<br>2.82 s | elevenlabs/eleven-v4-turbo<br>1.15 s | CLI 3.84 s; Teams not yet timed (est. ~7–8 s incl. travel) | 7.65 s |

Row 7 bench source: 5 Nov, 17 turns over 301 s, all fixture checks passed
(`session-separate-20261105T152427745844Z.json`). Thinking stage average
includes turns that ran bash tools. The thinking model (2.82 s) is now the
biggest single chunk of the first-word delay; the ElevenLabs voice is the
smallest (1.15 s).

Voice-stage note: the measured path streams raw PCM straight from OpenRouter's
ElevenLabs endpoint (confirmed `audio/pcm` 5 Nov) — no ffmpeg in it. ffmpeg
decode exists only as a fallback for endpoints that reject PCM and adds
~0.2 s decode plus a full-download wait when it triggers; it was in the path
briefly in the first live build before the PCM switch.

- Env "CLI" = command line with a recorded test voice, no Teams call.
- Env "Teams" = inside a real Teams meeting, which adds ~0.5–1 s of travel.
- "none" in the Avg column = that configuration was never run through a full 20-question session.
- Same model in all three stage columns = one model that listens, thinks, and speaks by itself.

Gap: rows 1, 3–5 were never tested in Teams, row 6 never at the CLI; rows 2
and 7 now have CLI numbers and live Teams evidence (row 2 fully timed live,
row 7's live delay still component-estimated). Teams transport is small;
host-side network calls dominate.

Bench note: the repeat-suppression meant for live meetings misfires on the
fixture plan (it repeats similar questions on purpose); it was disabled for
bench runs only.

Not committed.