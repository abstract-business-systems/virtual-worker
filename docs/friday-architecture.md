# Friday voice architecture — live Teams build (as of 2026-11-05)

One voice worker ("Friday") joins a Teams meeting in an Edge browser on the
shared host `abs-worker` (headless virtual display). Audio flows through
dedicated PulseAudio buses so the meeting and the worker never use the RDP
devices. All three model calls are billed through a single OpenRouter account.
Every turn is written to a JSON session report with timings and costs.

## Diagram

```mermaid
flowchart LR
    MEETING["Teams meeting<br/>Edge browser on abs-worker<br/>virtual display :99"]

    subgraph AUDIO["PulseAudio buses (abs-worker)"]
        direction TB
        CAP["vw_speaker.monitor<br/>capture bus (what the meeting plays)"]
        PLAY["vw_tts sink to output.vw_mic<br/>becomes Edge's microphone"]
    end

    subgraph FRIDAY["Friday process: chat_audio.py, preset 'separate'"]
        direction TB
        VAD["1. Endpointing<br/>pipecat Silero VAD (local ONNX)<br/>~0.5-0.8 s of silence commits the turn"]
        STT["2. Speech-to-text<br/>ElevenLabs Scribe v2<br/>via OpenRouter /audio/transcriptions"]
        BRAIN["3. Reasoning<br/>openai/gpt-6-luna via OpenRouter<br/>tools: bash, background jobs,<br/>read-only worker bridge"]
        TTS["4. Text-to-speech<br/>ElevenLabs v4 Turbo, voice Jessica<br/>via OpenRouter /audio/speech<br/>streams PCM while downloading"]
        SPK["5. Speaker feed<br/>24 kHz PCM to the vw_tts sink"]
        MEM["Memory + repeat suppression<br/>echo cooldown"]
        REPORT["Session report JSON<br/>turns, stage timings, costs"]
    end

    OR[("OpenRouter<br/>one account, one bill")]

    MEETING -- "meeting audio out" --> CAP
    CAP --> VAD
    VAD -- "user text" --> STT
    STT -- "transcript" --> BRAIN
    BRAIN <--> MEM
    BRAIN -- "answer text" --> TTS
    TTS -- "streamed audio" --> SPK
    SPK --> PLAY
    PLAY -- "Friday's microphone" --> MEETING
    STT -. "REST" .-> OR
    BRAIN -. "REST" .-> OR
    TTS -. "REST" .-> OR
    BRAIN -- "every turn" --> REPORT
```

## Component notes

| Box | Implementation | Network |
|---|---|---|
| Endpointing | pipecat `SileroVADAnalyzer` (local ONNX model) + pipecat resampler | none (local) |
| Speech-to-text | POC code calling `POST /audio/transcriptions` | OpenRouter |
| Reasoning + tools | POC `SharedReasoner` (`poc_reasoning.py`), bash with process-group kill and deadlines, managed background jobs (`poc_jobs.py`) | OpenRouter |
| Text-to-speech | POC code calling `POST /audio/speech`, raw PCM streamed during download; ffmpeg-decode fallback for endpoints without PCM support | OpenRouter |
| Reports | POC `poc_report.py` — per-turn timings, per-stage costs, unknown costs kept explicitly unknown | none |

## Known weak spot

The audio-bus layer is the fragile part: PulseAudio streams drift (Edge slides
back to the RDP devices, Friday's capture stream can disappear) and there is no
watchdog yet to restore routing or reconnect capture automatically. When that
happens Friday goes deaf until manually restarted.

## Alternates measured (same skeleton, different services)

- Realtime mode (`chat.py`): runs entirely inside pipecat's pipeline with
  `OpenAIRealtimeLLMService` — fastest round trip measured (~4.3 s).
- Separate mode with OpenAI-direct models: ~4.6 s round trip.
- Current ElevenLabs + Luna configuration: ~7–8 s round trip (measured), with
  streaming TTS shaving the tail.