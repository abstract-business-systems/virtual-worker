#!/usr/bin/env python3
"""M7 isolated POC probe: OpenAI realtime model (gpt-realtime-2.1-mini).

OpenAI-direct websocket Realtime API (NOT OpenRouter — realtime models are
OpenAI-direct only). No framework, no worker code. Measures:
  - server-side end-of-turn detection (speech_started/speech_stopped timings)
  - end of speech -> first audio chunk (vs. 7.77 s worker baseline)
  - audio streaming pattern
  - tool calling inside the voice session
  - actual usage/cost

Loads .env from this folder first (dedicated key), else environment vars.
Writes results/<timestamp>.json; saves reply audio to results/answer.wav.
Requires the `websockets` package.
"""

import base64
import json
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RESULTS = HERE / "results"
FIXTURES.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency: pip install websockets")


def load_env():
    """Load .env from this folder (dedicated key), then fall back to env vars."""
    cfg = {}
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    def get(k, default=None):
        return cfg.get(k) or os.environ.get(k) or default
    return get


def b64_pcm_to_wav(pcm_bytes, path, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)


def main():
    get = load_env()
    key = get("OPENAI_API_KEY")
    if not key:
        sys.exit(f"No API key: paste OPENAI_API_KEY into {HERE/'.env'}")
    model = get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
    voice = get("OPENAI_REALTIME_VOICE", "alloy")

    qpath = FIXTURES / "question.wav"
    if not qpath.exists():
        # reuse the generated question from the chat-completions probe
        alt = HERE.parent / "audio-model-poc" / "fixtures" / "question.wav"
        if alt.exists():
            qpath.write_bytes(alt.read_bytes())
        else:
            sys.exit("No question audio: run ../audio-model-poc/probe.py first "
                     f"or place a question at {qpath}")
    qwav = qpath.read_bytes()
    # pcm16 24kHz mono payload = strip the 44-byte WAV header
    qpcm = qwav[44:] if qwav[:4] == b"RIFF" else qwav
    qchunks = [qpcm[i:i + 9600] for i in range(0, len(qpcm), 9600)]  # 200 ms each

    run = {"model": model, "voice": voice,
           "started_utc": datetime.now(timezone.utc).isoformat(),
           "baseline_first_audio_s": 7.77,
           "question_file": str(qpath)}

    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {key}"}  # GA realtime: no beta header

    events = []          # (t, type) log of all server events
    metrics = {}

    async def run_probe():
        nonlocal metrics
        import asyncio
        t0 = time.perf_counter()
        async with websockets.connect(url, additional_headers=headers,
                                      max_size=50 * 1024 * 1024) as ws:
            async def send(obj):
                await ws.send(json.dumps(obj))

            # 1) session: audio in/out + server VAD + a tool
            await send({"type": "session.update", "session": {
                "type": "realtime",
                "instructions": ("You are Friday, a virtual worker. Acknowledge "
                                 "briefly, then call get_meeting_context before "
                                 "answering."),
                "audio": {
                    "input": {"format": {"type": "audio/pcm", "rate": 24000},
                              "turn_detection": {"type": "semantic_vad"}},
                    "output": {"format": {"type": "audio/pcm", "rate": 24000},
                               "voice": voice},
                },
                "tools": [{"type": "function", "name": "get_meeting_context",
                           "description": "Look up recent meeting notes and calendar "
                                          "context for Friday.",
                           "parameters": {"type": "object", "properties": {},
                                          "required": []}}],
            }})

            # 2) stream the question; let server VAD detect end-of-turn
            speech_stopped_at = None
            first_audio_at = None
            audio_parts = []
            tool_call = None
            texts = []
            usage = None

            async def reader():
                nonlocal speech_stopped_at, first_audio_at, tool_call, usage
                async for raw in ws:
                    ev = json.loads(raw)
                    et = ev.get("type", "?")
                    log = {"t": round(time.perf_counter() - t0, 3),
                           "type": et}
                    if et == "error":
                        log["payload"] = ev.get("error")
                    events.append(log)
                    if et == "input_audio_buffer.speech_stopped":
                        speech_stopped_at = time.perf_counter() - t0
                    elif et in ("response.audio.delta",
                                "response.output_audio.delta"):
                        d = ev.get("delta") or ev.get("audio") or ""
                        if d:
                            if first_audio_at is None:
                                first_audio_at = time.perf_counter() - t0
                            audio_parts.append(d)
                    elif et in ("response.audio_transcript.delta",
                                "response.output_audio_transcript.delta"):
                        texts.append(ev.get("delta") or "")
                    elif et in ("response.function_call_arguments.done",
                                "response.done"):
                        for item in (ev.get("response", {}).get("output") or []):
                            if item.get("type") == "function_call":
                                tool_call = {"name": item.get("name"),
                                             "args": (item.get("arguments") or "")[:200]}
                        if ev.get("response", {}).get("usage"):
                            usage = ev["response"]["usage"]
                    elif et in ("error", "invalid_request_error"):
                        metrics["error"] = ev
                        break
                    if et == "response.done":
                        break

            # send input audio, then request response (belt & braces for VAD)
            for i, c in enumerate(qchunks):
                await send({"type": "input_audio_buffer.append",
                            "audio": base64.b64encode(c).decode()})
            await send({"type": "input_audio_buffer.commit"})
            await send({"type": "response.create"})

            t_send_done = time.perf_counter() - t0
            await asyncio.wait_for(reader(), timeout=90)

            m = {
                "t_last_input_chunk_s": round(t_send_done, 3),
                "t_speech_stopped_s": round(speech_stopped_at, 3) if speech_stopped_at else None,
                "t_first_audio_chunk_s": round(first_audio_at, 3) if first_audio_at else None,
                "audio_chunk_count": len(audio_parts),
                "audio_bytes_b64": sum(len(p) for p in audio_parts),
                "tool_call": tool_call,
                "transcript": "".join(texts)[:400],
                "usage": usage,
                "event_count": len(events),
            }
            if audio_parts:
                pcm = base64.b64decode("".join(audio_parts))
                out = RESULTS / "answer.wav"
                b64_pcm_to_wav(pcm, out)
                m["answer_file"] = str(out)
            metrics = m

    import asyncio
    asyncio.run(run_probe())

    run["metrics"] = metrics
    run["events"] = events
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / f"run-{stamp}.json"
    out.write_text(json.dumps(run, indent=2))
    print(json.dumps(metrics, indent=2)[:1200], flush=True)
    print(f"results -> {out}", flush=True)


if __name__ == "__main__":
    main()