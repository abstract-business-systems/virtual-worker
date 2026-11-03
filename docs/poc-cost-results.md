# Three-mode POC — measured costs & latency (3 Nov 2025)

All runs end-to-end (mic replaced by `fixtures/question.wav`, bash + context
tools live, token accounting per turn). Exchange rate ~₹95/USD; monthly
basis: one agent listening ~8 h/day ≈ 190 meeting-hours.

## Measured per turn — with cache breakdown

| Mode / turn | First audio | In tokens (audio) | Cached (text/audio) | Out tokens (audio) | Cost/turn | Cache discount |
|---|---|---|---|---|---|---|
| realtime T1 | **1.13 s** | 524 (75) | 0 | 226 (118) | ~$0.0036 | — |
| realtime T2 | 1.13 s | 877 (75) | **512 (448/64)** — 58% of input | 341 (192) | ~$0.0056 | **saved ~$0.0005 (~8%)** |
| audio | 3.28 s | 551 (162) | n/a (per-turn call, no cache) | 233 (170) | ~$0.0009 | — |
| separate | 4.74 s | 356 (81 — STT) | n/a | 428 (288) | ~$0.0018 | — |

Reading the numbers: input text tokens = system prompt + conversation
history (this is what caching discounts); input audio = seconds heard ×
~12 tokens/s; output audio = spoken reply × ~15 tokens/s. Cache grows with
conversation length — by turn 10 the discount is far larger than 8%.

Separate stage timings (fresh run): stt 1.59 s · think 2.12 s · tts 2.51 s.
Separate per-stage cost: STT ~$0.0008 (81 audio tokens) · think ~$0.0001 ·
TTS ~$0.0009 — the voice makes most of the bill, as in audio mode.

## Latency breakdown — real life (you stop speaking → reply starts)

| Mode | Where the time goes | Expect end-to-end |
|---|---|---|
| realtime | one server stream: turn detection + generation + first audio chunk (no endpointing stage) | **1.1–1.6 s** |
| audio | endpointing pause ~0.5–0.8 s + one OpenRouter call (understand + speak in one model) | **~2.6–3.5 s** |
| separate | endpointing + stt 0.9 s → think 2.1 s → first tts chunk ~2.6 s (stages run back-to-back) | **~4.1–5.5 s** |

Realtime's advantage is structural: it detects the turn and starts speaking
without waiting for a silence threshold or three sequential calls.

## Cost in real-life scenarios

| Scenario | realtime | audio | separate |
|---|---|---|---|
| One Q&A turn (typical, cached) | $0.004–0.006 | $0.0009 | $0.0018 (STT included) |
| Meeting-hour (Friday listening 60 min, speaking ~10 min) | **~₹51** | **~₹4** | **~₹4–6 (STT included)** |
| Month (190 meeting-hours) | **~₹9,800** | **~₹780** | **~₹800–1,200** |

Why realtime is 12× dearer: it bills **audio-in for everything heard,
continuously** — silence, other people talking, everything. Audio and
separate only bill while someone is actually uttering. That's why per-turn
× turn-count undercounts realtime; the meeting-hour model is the honest
comparison.

*STT pricing*: the transcription call's usage is captured (81 audio
tokens in the measured turn), but it is billed through the generic
audio-in bucket rate ($10/M) — conservative; the actual
gpt-4o-mini-transcribe list rate is likely lower. Reconcile with a real
bill before quoting a monthly number.

## Pricing assumptions (bucket-priced usage — realtime path)

| Bucket | Rate /M tokens |
|---|---|
| audio in / out | $10 / $20 (assumed mini-family; verify vs bill) |
| text in / out | $0.60 / $2.40 (assumed) |
| cached in (text/audio) | 50% of above (`CACHE_DISCOUNT`, token_accounting.py) |

OpenRouter paths (audio, separate think/TTS) use provider-computed cost —
exact, no assumptions.

## Verdict — VC's question (M8): "if you pay less, what do we get? if you pay more?"

| | **Pay more — realtime** (~₹9,800/mo) | **Pay less — audio single-call** (~₹780/mo) | **Middle — separate cascade** (~₹800–1,200) |
|---|---|---|---|
| First audio | **1.1–1.6 s** | 2.6–3.5 s | 4.1–5.5 s |
| Feel (hands-on verdict) | **Clearly best** — natural turn-taking, mid-voice tools, no abrupt endings | Works, noticeably slower; approximate memory | Works, slowest — three sequential stages are audible |
| Reliability | Session dies on any network drop (needs reconnect+resume work) | **Uniquely survives network drops** (HTTP per turn, self-heals) | Survives drops like audio mode |
| Known quirks | 12× cost; OpenAI-locked | Tool-call fragility (rejected `tool_calls` after audio, worked around); stochastic skips | Needs online STT for accuracy; STT priced at the conservative bucket rate (reconcile) |
| One line | Best experience, premium price | 12× cheaper, ~1–2.5 s slower, fragile tools | Clean roles, native tools, too slow to feel instant |

**Bottom line:** realtime is the one that works best and costs 12× more; the
cheap paths work but are measurably slower and (single-call) tool-fragile.
Full decision material and open questions:
[`architecture-discussion.md`](architecture-discussion.md). Lean
recommendation there: Pipecat + realtime as primary, cheap path as
documented fallback.

## Verification status

- All three modes: full turn + clean Ctrl+C exit + `results/*.json` report ✓
- Session report JSON serializable; cache buckets in per-turn + totals ✓
- `stop.sh` stops every instance (graceful SIGINT first) ✓