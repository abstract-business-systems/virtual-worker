# Friday voice architecture — live Teams build (as of 2026-11-05)

One voice worker ("Friday") joins a Teams meeting in an Edge browser on the
shared host `abs-worker` (headless virtual display). Audio flows through
dedicated PulseAudio buses so the meeting and the worker never use the RDP
devices. All three model calls are billed through a single OpenRouter account.
Every turn is written to a JSON session report with timings and costs.

The architecture is split into four small diagrams: the overall conversation
loop, the Friday internals, the audio transport, and the reporting.

## 1. Overall loop (the conversation cycle)

```mermaid
flowchart TB
    A["You speak in the Teams meeting"]
    B["Friday listens (capture bus)"]
    C["Friday thinks and answers"]
    D["Friday speaks into the meeting (as its microphone)"]
    A --> B --> C --> D --> A
```

One turn: your speech is captured, transcribed, reasoned about, and spoken
back — then Friday keeps listening.

## 2. Friday internals (the five stages, one process)

```mermaid
flowchart TB
    V["1. Endpointing<br/>pipecat Silero VAD (local ONNX)<br/>~0.5-0.8 s of silence commits the turn"]
    S["2. Speech-to-text<br/>ElevenLabs Scribe v2<br/>via OpenRouter"]
    R["3. Reasoning<br/>openai/gpt-6-luna<br/>via OpenRouter"]
    T["4. Text-to-speech<br/>ElevenLabs v4 Turbo, voice Jessica<br/>via OpenRouter, streamed PCM"]
    P["5. Speaker feed<br/>24 kHz PCM"]
    V --> S --> R --> T --> P
```

The reasoning stage also carries tools and memory:

```mermaid
flowchart TB
    R["Reasoning<br/>openai/gpt-6-luna"]
    M["Memory + repeat suppression<br/>echo cooldown"]
    J["bash with deadlines<br/>managed background jobs"]
    W["read-only worker bridge"]
    R <--> M
    R --> J
    R --> W
```

## 3. Audio transport (how audio physically moves)

```mermaid
flowchart TB
    MEETING["Teams meeting<br/>Edge browser on abs-worker<br/>virtual display :99"]
    CAP["vw_speaker.monitor<br/>capture bus:<br/>what the meeting plays out"]
    PLAY["vw_tts sink, then output.vw_mic<br/>becomes Edge's microphone"]
    MEETING --> CAP
    PLAY --> MEETING
```

Known weak spot: PulseAudio streams drift here (Edge slides back to the RDP
devices, Friday's capture stream can disappear) and there is no watchdog yet —
when it happens, Friday goes deaf until manually restarted.

## 4. Reporting and billing

```mermaid
flowchart TB
    S["Speech-to-text"]
    R["Reasoning"]
    T["Text-to-speech"]
    OR[("OpenRouter<br/>one account, one bill")]
    REP["Session report JSON<br/>turns, stage timings, costs<br/>unknown costs kept unknown"]
    S --> OR
    R --> OR
    T --> OR
    R -- "every turn" --> REP
```

## Alternates measured (same skeleton, different services)

- Realtime mode (`chat.py`): runs entirely inside pipecat's pipeline with
  `OpenAIRealtimeLLMService` — fastest round trip measured (~4.3 s).
- Separate mode with OpenAI-direct models: ~4.6 s round trip.
- Current ElevenLabs + Luna configuration: ~7–8 s round trip (measured), with
  streaming TTS shaving the tail.