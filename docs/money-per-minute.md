# Money per minute / per session (M9 #19)

All from measured sessions (`docs/poc-cost-results.md` same assumptions). FX $1 ≈ ₹84.
"Meeting-minute" = blended minute of a meeting Friday attends (listening + occasional replies);
"speaking-minute" = a minute in which Friday is actively replying (turns back-to-back).

## Cost ladder per mode

| Unit | realtime | audio (mini) | audio (full) | separate |
|---|---|---|---|---|
| One turn (measured, avg) | $0.0046 | $0.0009 | $0.0213 | $0.0026 |
| Speaking-minute (~turns back-to-back) | ₹2.7 | ₹0.57 | ₹13.4 | ₹1.1 |
| Meeting-minute (blended, meeting-hour basis) | ₹0.85 | ₹0.067 | ~₹1.6 | ₹0.07–0.10 |
| 25-min session | ₹21 | ₹1.7 | ~₹40 | ₹1.8–2.5 |
| Day (8 × 25-min sessions) | ₹171 | ₹13.5 | ~₹320 | ₹14–20 |
| Month (190 meeting-hours) | **₹9,800** | **₹780** | **~₹30,000** | **₹800–1,200** |

## Cache effect (already inside the numbers)

| Mode | Cache | Effect on per-turn |
|---|---|---|
| realtime | 58% of prompt audio+text cached (measured T2) | $0.0046 is the *cached* rate; uncached ≈ $0.008 |
| audio | no audio cache (chat-completions voice) | n/a — text part could cache, audio dominates cost |
| separate | think-model text cache (OpenRouter reports single number) | tiny — text prompt is small; STT/TTS are per-utterance |

## Reading

- Per-minute during speaking, separate ≈ 2× cheaper than realtime; but realtime bills *listening* too, which is where its 12× meeting-hour gap comes from.
- Audio-mini is the per-minute winner but mishears (M9); audio-full hearing is perfect at ~24× the price (₹13.4/speaking-min) — breaks the ₹800–900/month target on its own.
- Separate at ₹800–1,200/month meets the target *if* quality (mishearing) is fixed — it is the current primary candidate.