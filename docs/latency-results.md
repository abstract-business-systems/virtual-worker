# Voice latency results (5 Nov 2026)

"First word" = you finish speaking → the agent's first spoken word reaches you.

| # | Configuration | Env | Pause wait | Speech-to-text | Thinking | Voice | First word | Avg over 20 q |
|---|---|---|---:|---|---|---|---:|---:|
| 1 | Original worker | CLI | 2.01 s | Whisper (local)<br>1.54 s | gpt-4o-mini<br>1.93 s | local<br>2.45 s | 7.77 s | 20.32 s (whole reply, not first word) |
| 2 | Realtime | CLI | one connection does all three | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | gpt-realtime-2.1-mini<br>one connection | 1.13 s | 4.30 s |
| 3 | One audio model, full | CLI | 0.7 s | gpt-audio<br>0 s, inside same call | gpt-audio<br>inside same call | gpt-audio<br>2.6 s | 3.28 s | 5.85 s |
| 4 | One audio model, small | CLI | 0.7 s | gpt-audio-mini<br>0 s, inside same call | gpt-audio-mini<br>inside same call | gpt-audio-mini<br>2.6 s | 3.28 s | none |
| 5 | Three calls, OpenAI direct | CLI | 0.5 s | gpt-4o-mini-transcribe<br>1.59 s | gpt-4o-mini<br>2.12 s | gpt-4o-mini-tts<br>1.0 s | 4.74 s | 4.56 s |
| 6 | Three calls, OpenRouter voice | Teams | never measured | gpt-audio<br>never measured | gpt-6-luna<br>never measured | gpt-audio<br>never measured | not measured | none |
| 7 | Three calls, ElevenLabs (today's Friday) | Teams | 0.5–0.8 s | elevenlabs/scribe-v2<br>2.62 s | gpt-6-luna<br>2.58 s | elevenlabs/eleven-v4-turbo<br>1.0 s | 7–8 s (+0.5–1.0 s Teams travel) | none |

- Env "CLI" = command line with a recorded test voice, no Teams call.
- Env "Teams" = inside a real Teams meeting, which adds ~0.5–1 s of travel.
- "none" in the Avg column = that configuration was never run through a full 20-question session.
- Same model in all three stage columns = one model that listens, thinks, and speaks by itself.

Gap: rows 1–5 were never tested in Teams, rows 6–7 never at the CLI —
so the Teams overhead is estimated, not measured both ways.

Not committed.