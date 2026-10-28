# Virtual Worker — End-to-End Latency Findings

Status: preliminary measurement for later discussion  
Measurement date: 28 October 2026  
Source implementation: local worker prototype on `abs-worker`  
Trials: 5 successful question/answer interactions

## Executive summary

The current worker takes a median of **8.25 seconds** from detected participant
speech to the start of the first spoken response. The measured P95 is **9.77
seconds**.

If the complete response is considered, including all generated speech, the
median is **21.07 seconds** and the P95 is **25.54 seconds**. This second
number is strongly affected by response length and audio playback duration; the
first-audio measurement is the more useful measure of conversational
responsiveness.

No cost measurement or latency optimization was performed as part of this
exercise.

## What was measured

The benchmark used the worker's local end-to-end audio path:

```text
participant test speech
  -> PulseAudio meeting bus
  -> speech endpointing
  -> faster-whisper STT
  -> backend context lookup
  -> OpenRouter answer model
  -> streamed response
  -> text-to-speech synthesis
  -> virtual microphone playback
```

The benchmark was run through the question-only command:

```text
vw latency 5
```

The participant utterances were generated speech played into the meeting audio
bus. This is a controlled local benchmark and does **not** yet represent a
live Teams network measurement.

## Aggregate results

All values are milliseconds.

| Measurement | Minimum | P50 | P95 | Maximum |
|---|---:|---:|---:|---:|
| Speech detected to first response audio | 7,939.2 | **8,252.3** | **9,770.4** | 9,770.4 |
| Speech detected to response completion | 13,427.0 | **21,070.3** | **25,536.6** | 25,536.6 |
| Speech capture, endpointing, and STT | 3,090.7 | **3,642.1** | **5,470.3** | 5,470.3 |
| STT model processing only | 1,505.1 | **1,636.4** | **1,672.8** | 1,672.8 |
| Context lookup | 1.4 | **1.6** | **2.2** | 2.2 |
| Model request to first token | 1,566.4 | **1,885.0** | **2,311.1** | 2,311.1 |
| First TTS synthesis | 2,464.4 | **2,613.2** | **3,044.8** | 3,044.8 |
| Audio playback after first response audio | 5,487.8 | **12,451.0** | **17,055.4** | 17,055.4 |

The percentile calculation uses the nearest-rank P95. With five samples, the
P95 is therefore the slowest measured sample.

## Individual trials

| Trial | Recognized question | First response audio | Response complete |
|---:|---|---:|---:|
| 1 | “what did we agree about” | 8.48 s | 25.54 s |
| 2 | “who will prepare the report” | 7.94 s | 13.43 s |
| 3 | “where is the lunch” | 8.14 s | 20.59 s |
| 4 | “what is the main decision” | 9.77 s | 21.07 s |
| 5 | “what is the lunch plan” | 8.25 s | 23.61 s |

## Findings

1. **The first response is noticeably slow.** The worker does not begin
   speaking until roughly eight to ten seconds after speech is detected.
2. **Context lookup is not a bottleneck.** It consistently takes about two
   milliseconds.
3. **Speech capture and endpointing contribute materially.** The combined
   capture, silence detection, and STT phase has a median of 3.64 seconds.
4. **The answer model is a secondary contributor.** Time from model request to
   the first token has a median of 1.89 seconds and a P95 of 2.31 seconds.
5. **Initial TTS synthesis is also significant.** The first response audio
   waits a median of 2.61 seconds for synthesis.
6. **Full-response latency is dominated by response length.** Once the first
   audio begins, playback can continue for several additional seconds. The
   completion P95 was 25.54 seconds.
7. **Speech recognition quality affected the sample.** The test intended to ask
   about the launch plan, but STT recognized “launch” as “lunch” in the final
   question. This is recorded as a benchmark quality observation, not treated
   as a latency failure.

## Timing definitions

- **Speech detected:** the local audio segmenter detects the beginning of a
  speech segment.
- **Speech detected to first response audio:** time until the first synthesized
  response WAV begins playback into the worker's virtual microphone.
- **Speech detected to response completion:** time until all generated response
  audio finishes playback.
- **Speech capture, endpointing, and STT:** includes the utterance duration,
  configured silence endpoint, and STT processing. It is intentionally broader
  than STT model time alone.
- **Model request to first token:** time from starting the streamed answer
  request until the first model text delta arrives.
- **First TTS synthesis:** time needed to synthesize the first response sentence
  after model text becomes available.

## Instrumentation changes

The measurement added timestamp tracing at the following boundaries:

- speech segment start, endpoint, and STT start/finish;
- context request start/finish;
- model request start, first token, and finish;
- first TTS start/finish;
- first audio playback start;
- complete response playback finish.

The trace is emitted as a single `[latency]` JSON record in the agent service
log for every measured question. The benchmark command returns the individual
records and aggregate statistics.

## Scope and limitations

- This was a five-trial controlled local run, not a live Teams call.
- The participant side used generated test speech rather than a human speaker.
- Network conditions, browser audio capture, meeting-server transport, and
  real participant interruptions were not separately isolated.
- The benchmark measures response start and completion, but does not define an
  acceptable product latency target.
- This document records findings only; no latency optimization was performed.
