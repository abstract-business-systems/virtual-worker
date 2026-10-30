# Virtual Worker — Conversation Layer: Open-Source Options

Source: M4 and M5 conversation-layer decisions (see
[`meetings.md`](meetings.md)) and the validation recorded in
[`next-steps.md`](next-steps.md).

Status: research only. No framework has been selected and no code has been
changed. Any adoption must be benchmarked against the documented first-audio
baseline in [`latency-findings.md`](latency-findings.md).

## What the conversation layer needs

From M4 and M5, the conversation layer must provide:

1. Continuous speech processing and endpointing (listening / waiting / acting).
2. An immediate acknowledgement while the content task runs in the background.
3. Interruption and barge-in: stop playback, drop or reconsider the task.
4. Streaming STT and streaming TTS (enabling work for the acknowledgement).
5. Progress updates for long-running tasks.

The current pipeline implements none of this orchestration: endpointing is
energy-based, STT is batch faster-whisper, and TTS waits for a complete
first-sentence WAV.

## Full frameworks

| Framework | License | Activity | Assessment |
|---|---|---|---|
| [Pipecat](https://github.com/pipecat-ai/pipecat) | BSD-2-Clause | Very active (~17k stars) | **Best fit.** Python pipeline with turn detection, interruption/barge-in, streaming STT→LLM→TTS, and background function calling. Transport-agnostic: the browser/Teams virtual-microphone path can plug in as a custom transport. Supports local faster-whisper STT, ElevenLabs TTS, and OpenAI-compatible LLM endpoints (OpenRouter via base URL). |
| [LiveKit Agents](https://github.com/livekit/agents) | Apache-2.0 | Very active (~15k stars) | Equivalent capabilities, including an open turn-detection model, but assumes LiveKit WebRTC rooms. Bridging browser audio into LiveKit adds infrastructure without benefit for this setup. |
| [TEN Framework](https://github.com/TEN-framework/ten-framework) | Custom | Active (~11k stars) | Graph-based with a Go/C++/JS core. More powerful for complex agents but heavier and harder to pair with the thin Python agent. |
| Vocode | — | Repo no longer available | Excluded. |

## Component-level alternatives

| Component | License | Replaces |
|---|---|---|
| [RealtimeSTT](https://github.com/KoljaB/RealtimeSTT) | MIT, very active | The whole `stt.py` stage: streaming faster-whisper with VAD and endpointing. Attacks the ~2 s endpointing and ~1.5 s STT stages directly. |
| [Silero VAD](https://github.com/snakers4/silero-vad) | MIT, very active | The energy-based endpointing: a small ONNX voice-activity model, CPU-friendly. |
| [Smart-turn](https://github.com/pipecat-ai/smart-turn) | BSD-2-Clause | The "did the user finish?" decision — the listening/waiting/acting boundary from M5. Small ONNX model, runs on CPU. |
| [WhisperLive](https://github.com/collabora/WhisperLive) / [whisper_streaming](https://github.com/ufal/whisper_streaming) | MIT | Streaming STT servers. Viable but heavier deployment than RealtimeSTT. |

## Pipecat deep-dive (verified from the repository, 2026-10-29)

Checked against the live `pipecat-ai/pipecat` repo (BSD-2-Clause, very active,
Python ≥ 3.11, production/stable):

- **Local audio transport is first-class** (`src/pipecat/transports/local/`),
  so the virtual-mic/Teams setup needs no Daily or LiveKit infrastructure —
  those are optional extras, not core.
- **Dedicated OpenRouter LLM service** (`src/pipecat/services/openrouter/`)
  exists, beyond the generic OpenAI-compatible base URL.
- **Silero VAD is bundled** (ONNX model in-repo) and **smart-turn v3
  (local, ONNX) end-of-turn detection is built in** — `onnxruntime` is a core
  dependency, so endpointing and turn detection work on CPU with no extra
  setup.
- **Core dependency footprint is modest**: common libraries (numpy, pydantic,
  aiohttp, openai SDK, websockets, onnxruntime). No mandatory cloud SDKs.
- **Speech-to-speech services** (OpenAI Realtime, Gemini Live, AWS Nova
  Sonic, Azure Voice Live, Grok Voice, Ultravox) are separate websocket
  integrations — note these are *not* chat-completions audio models, so
  Option D (gpt-audio-mini via chat completions) would still be a custom
  processor inside Pipecat, or a direct-API probe outside it.
- Ships a CLI (`pipecat init`) and quickstart for scaffolding a demo bot in
  minutes — relevant to the M6 demo gate.
- `Pipecat Flows` provides structured conversation/state management, which
  partially overlaps our planned state machine.

Conclusion for the option decision: with the local transport, bundled VAD,
built-in smart-turn, and the OpenRouter service, the integration is mostly
configuration plus one conversation-state processor — the integration sketch
in [`conversation-layer-plan.md`](conversation-layer-plan.md) stands, with
less custom code than assumed. The demo comparison is then: raw
single-model probe (Option D) vs. the same flow wrapped in Pipecat
(Option A).

### Model swapping (configuration, not customization)

Model/provider changes are pure configuration: the pipeline is a list of
service objects, so changing a model is changing a string (`model="..."`) and
changing a provider is swapping one service class while the rest of the
pipeline is untouched. The README lists 25+ STT, 30+ LLM, and 35+ TTS
services, including the current stack (local Whisper, ElevenLabs, OpenRouter).
We would wrap service creation in the existing `config.py` environment
pattern (`VW_STT_MODEL`, `VW_LLM_MODEL`, `VW_TTS_MODEL`) so swaps become
config changes, not code changes. Caveat: each provider has its own latency
and cost profile, so swaps still warrant a quick re-measurement via the
planned demo harness.

## Recommendation

Adopt **Pipecat** as the conversation layer. It already ships the orchestration
M4 and M5 described — interruption, turn management, streaming, background
tasks — and works with the existing providers (faster-whisper, ElevenLabs,
OpenRouter) and the browser/Teams audio path via a custom transport.

Fallback if a framework dependency is not acceptable: integrate
**RealtimeSTT + Silero VAD + smart-turn** into the current agent and write the
state/interruption logic ourselves. Lower framework risk, but the
orchestration must be built by hand.

## Evaluation requirements for either path

1. Benchmark against the documented **7.77 s median first-audio** baseline
   under identical test conditions, before and after integration.
2. Verify the acknowledgement can start sounding in roughly 1–2 seconds with
   streaming STT and streaming TTS, against the ~3.5–5 s bound documented in
   [`next-steps.md`](next-steps.md).
3. Verify interruption and barge-in behavior over the Teams audio path.
4. No provider, model, region, or endpoint changes are included in this scope.