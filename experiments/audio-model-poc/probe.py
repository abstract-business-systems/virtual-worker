#!/usr/bin/env python3
"""M7 isolated POC probe: gpt-audio-mini via OpenRouter.

No framework, no worker code. All calls stream (OpenRouter requires
stream=true for audio output). Measures:
  - step 1 (bootstrap): text -> audio out; first-chunk latency + usage
  - step 2 (audio-in): wav -> audio out + tool call, streaming:
      request -> first audio chunk, chunk pattern, tool calls, usage

Writes results/<timestamp>.json and saves audio to fixtures/ and results/.
Stdlib only. Requires OPENROUTER_API_KEY.
"""

import base64
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RESULTS = HERE / "results"
FIXTURES.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

MODEL = os.environ.get("VW_POC_MODEL", "openai/gpt-audio-mini")
VOICE = os.environ.get("VW_POC_VOICE", "alloy")
API = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY")
if not KEY:
    sys.exit("OPENROUTER_API_KEY not set")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_meeting_context",
            "description": "Look up recent meeting notes and calendar context for Friday.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def _stream(messages, tools=None):
    """Stream a chat completion; returns (metrics, usage, audio_parts, texts,
    tool_calls). All timing relative to request start."""
    payload = {
        "model": MODEL,
        "modalities": ["text", "audio"],
        "audio": {"voice": VOICE, "format": "pcm16"},
        "stream": True,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools

    m = {}
    t0 = time.perf_counter()
    b64parts, chunks, texts, tool_calls = [], [], [], []
    first_audio = first_text = usage = None
    try:
        req = urllib.request.Request(
            API,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {KEY}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            m["http_status"] = resp.status
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                s = line[5:].strip()
                if s == "[DONE]":
                    break
                try:
                    ev = json.loads(s)
                except json.JSONDecodeError:
                    continue
                now = time.perf_counter() - t0
                if ev.get("usage"):
                    usage = ev["usage"]
                ch = (ev.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                ad = (delta.get("audio") or {}).get("data")
                if ad:
                    if first_audio is None:
                        first_audio = now
                    b64parts.append(ad)
                    chunks.append({"t": round(now, 3), "len": len(ad)})
                tr = (delta.get("audio") or {}).get("transcript")
                if tr:
                    texts.append(tr)
                c = delta.get("content")
                if c:
                    if first_text is None:
                        first_text = now
                    texts.append(c)
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    tool_calls.append({"name": fn.get("name"),
                                       "args": (fn.get("arguments") or "")[:200]})
                if ch.get("finish_reason"):
                    m["finish_reason"] = ch["finish_reason"]
    except Exception as e:  # noqa: BLE001 — record the failure, keep partial data
        m["stream_error"] = repr(e)

    m["t_first_audio_chunk_s"] = round(first_audio, 3) if first_audio else None
    m["t_first_text_chunk_s"] = round(first_text, 3) if first_text else None
    m["audio_chunk_count"] = len(chunks)
    if chunks:
        m["audio_chunk_span_s"] = round(chunks[-1]["t"] - chunks[0]["t"], 3)
        m["audio_bytes"] = sum(c["len"] for c in chunks)
        m["chunks"] = chunks[:60]
    m["transcript_or_text"] = "".join(texts)[:400]
    m["tool_calls"] = tool_calls
    m["usage"] = _usage(usage)
    return m, b64parts


def _usage(usage):
    if not usage:
        return None
    return {k: usage.get(k)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                      "cost", "prompt_tokens_details", "completion_tokens_details")
            if usage.get(k) is not None}


def main():
    run = {"model": MODEL, "voice": VOICE,
           "started_utc": datetime.now(timezone.utc).isoformat(),
           "baseline_first_audio_s": 7.77,
           "note": "OpenRouter requires stream=true for audio output (HTTP 400 otherwise)"}

    print("[1/2] bootstrap: text -> audio (streaming) ...", flush=True)
    m1, parts1 = _stream([
        {"role": "system", "content": "You are Friday, a virtual worker."},
        {"role": "user", "content": "Say exactly: Friday, what meetings do I have today?"},
    ])
    run["step1"] = m1
    if parts1:
        import wave
        pcm = base64.b64decode("".join(parts1))
        with wave.open(str(FIXTURES / "question.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(pcm)
        run["step1"]["question_file"] = str(FIXTURES / "question.wav")
    print(json.dumps({k: v for k, v in m1.items() if k != "chunks"}, indent=2)[:700],
          flush=True)

    qpath = run["step1"].get("question_file")
    if not qpath:
        print("bootstrap produced no audio; cannot run step 2", flush=True)
        run["step2"] = {"error": "no question audio generated"}
    else:
        print("[2/2] audio-in probe: wav -> audio + tool (streaming) ...", flush=True)
        qb64 = base64.b64encode(Path(qpath).read_bytes()).decode()
        m2, parts2 = _stream([
            {"role": "system", "content": (
                "You are Friday, a virtual worker. Acknowledge briefly, then "
                "use the get_meeting_context tool before answering."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": "Answer my spoken question."},
                {"type": "input_audio",
                 "input_audio": {"data": qb64, "format": "wav"}},
            ]},
        ], tools=TOOLS)
        if parts2:
            import wave
            pcm = base64.b64decode("".join(parts2))
            out = RESULTS / "answer.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(pcm)
            m2["answer_file"] = str(out)
        run["step2"] = m2
        print(json.dumps({k: v for k, v in m2.items() if k != "chunks"}, indent=2)[:900],
              flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / f"run-{stamp}.json"
    out.write_text(json.dumps(run, indent=2))
    print(f"results -> {out}", flush=True)


if __name__ == "__main__":
    main()