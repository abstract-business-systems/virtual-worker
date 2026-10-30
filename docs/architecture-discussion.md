# Friday conversation layer — architecture discussion

*Source: M7 experiments (`experiments/`) — three runnable, live-tested CLIs.
Context: the worker joins meetings, listens, builds transcript memory
([requirements.md](requirements.md)); this decision only replaces the
spoken-reply moment (perceived delay: 7.8 s median today). Scale: one
agent ~8 h/day ≈ 190 meeting-hours/month. Tested: Pipecat + OpenAI
realtime (`pipecat-poc/`), hand-built raw realtime (`realtime-model-poc/`),
hand-built + gpt-audio-mini via OpenRouter (`audio-model-poc/`) — all with
Silero VAD end-of-turn, barge-in, tools, presets in `models.ini`.*

## Measured
| Path | First audio | Cost/mo | Tools | Net-drop resilience |
|---|---|---|---|---|
| Pipecat + realtime | ~1.2 s | ~₹9,800 | native | session dies |
| Raw realtime | ~1.1–1.6 s | ~₹9,800 | native | session dies |
| Cheap single-call | ~2.4 s | ~₹780 | workaround | survives |
| Cheap cascade (voice+think) | ~3.5 s | ~₹800–900 | native | survives |

## Live verdicts (user, hands-on)
- Realtime model paths feel clearly better than the cheap audio path.
- **Pipecat's plumbing feels more natural** — no abrupt stop endings, it keeps listening properly; and the plumbing is model-independent, so it composes with any model (incl. voice/think presets).

## Verified findings
- Server-side turn detection never commits on `gpt-realtime-2.1-mini` (verified 4 ways) → client-side VAD mandatory everywhere.
- Framework adds no latency (A vs B within ~0.1 s); its value is robust turn-taking wiring — matching the feel verdict.
- Realtime sessions die on any network drop; HTTP-per-turn self-heals
  (production realtime needs reconnect + context-resume).
- OpenRouter audio fragility: rejects `tool_calls` after audio (worked around); stochastic skips permanent; OpenAI-direct costs the same as realtime — cheap price and fragility share one source.
- Model swapping is configuration, not code.

## Options
| Option | Trade-off summary |
|---|---|
| A: Pipecat + realtime | Fastest, best feel; 12× cost; reconnect logic; OpenAI-locked |
| C single-call | 12× cheaper, self-heals; ~1 s slower; fragile tools; approximate memory |
| C cascade | Clean roles, native tools, slowest (~3.5 s); needs real STT |

## Questions for the discussion
1. Is ~1 s faster worth 12× cost (₹9,800 vs ₹780/mo)? What delay do we sign up for?
2. Accept session-reconnect work for realtime, given real drop frequency?
3. Voice/think split (+1.2 s) for observability, or single-call simplicity?
4. OpenAI realtime lock-in acceptable; OpenRouter as hedge?
5. Adopt Pipecat as the layer (feel verdict at zero latency/cost premium)?
6. Next: wire the chosen flow into the worker's virtual-mic path with real tools.

## Lean (to react to)
**Pipecat + OpenAI realtime (A)** — best feel at identical measured
latency/cost to the raw layer; plumbing proven model-independent. Cheap
path kept as documented fallback. Open: cost × 12, reconnect work.