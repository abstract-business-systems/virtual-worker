#!/usr/bin/env python3
"""M7 POC — interactive CLI on the CHEAP path: gpt-audio-mini via OpenRouter
chat completions. The conversation layer is HAND-BUILT here (that is the
point of this POC — contrast with ../pipecat-poc/chat.py where Pipecat
provides it):

  mic (sounddevice, 100 ms blocks)
    -> Silero VAD (local ONNX) detects end-of-utterance  [hand-built]
    -> whole utterance sent as input_audio to chat completions
    -> streaming audio response played through speakers
    -> tool calls executed, follow-up request streamed
    -> conversation memory via message history  [hand-built]

No Pipecat, no realtime API. Requires OPENROUTER_API_KEY (same as backend).

Run: ./run-chat-audio.sh   (Ctrl+C or closing the terminal exits)
"""

import asyncio
import base64
import json
import os
import queue
import signal
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from pipecat.audio.utils import calculate_audio_volume, create_stream_resampler
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

HERE = Path(__file__).parent
IN_RATE = 16000          # VAD native rate
OUT_RATE = 44100         # typical playback device rate
MODEL = os.environ.get("VW_POC_MODEL", "openai/gpt-audio-mini")
VOICE = os.environ.get("VW_POC_VOICE", "alloy")
API = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY")
DEBUG = bool(os.getenv("CHAT_DEBUG"))

SYSTEM_PROMPT = (
    "You are Friday, a virtual worker. Keep spoken answers brief (2-3 "
    "sentences). Call get_meeting_context when the question touches "
    "meetings, notes, or the calendar."
)
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_meeting_context",
        "description": "Look up recent meeting notes and calendar context for Friday.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}]

if not KEY:
    sys.exit("OPENROUTER_API_KEY not set")

turn_seq = 0  # bumped on barge-in; in-flight turn audio with an older token is dropped


class Speaker:
    """24 kHz model audio -> 44.1 kHz playback (same callback as pipecat POC)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.playing = False
        self.stream = None
        self.resampler = None

    def _callback(self, outdata, frames, t, status):
        need = frames * 2
        chunks = []
        while need > 0:
            try:
                d = self.q.get_nowait()
            except queue.Empty:
                break
            d = d[: len(d) // 2 * 2]
            take = min(need, len(d))
            chunks.append(d[:take])
            need -= take
            if len(d) > take:
                self.q.queue.appendleft(d[take:])
        data = b"".join(chunks)
        outdata[:] = data.ljust(frames * 2, b"\x00")

    def start(self):
        self.resampler = create_stream_resampler()
        outdev = sd.query_devices(kind="output")
        rate = int(outdev["default_samplerate"])
        self.stream = sd.RawOutputStream(
            samplerate=rate, blocksize=1024, dtype="int16", channels=1,
            callback=self._callback)
        self.stream.start()
        self._rate = rate

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def barge_in(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.playing = False

    async def feed(self, pcm24: bytes):
        """Queue one chunk of 24 kHz model audio for playback."""
        audio = pcm24
        if self._rate != 24000:
            audio = await self.resampler.resample(pcm24, 24000, self._rate)
        if audio:
            self.q.put(audio)
            self.playing = True


def pcm_to_wav_b64(pcm24: bytes) -> str:
    """Wrap raw 24 kHz PCM16 in a real WAV container, base64-encoded.
    The provider rejects headerless PCM sent with format=wav (verified:
    'This model does not support the format you provided')."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm24)
    return base64.b64encode(buf.getvalue()).decode()


speaker = Speaker()
loop: asyncio.AbstractEventLoop | None = None
mic_q: asyncio.Queue | None = None


def mic_callback(indata, frames, t, status):
    if loop is not None and mic_q is not None:
        loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))


async def chat_stream(messages, on_audio, on_text):
    """Streaming chat completion on a worker thread (urllib is blocking).

    Returns (tool_calls, usage). on_audio(b64_pcm24) and on_text(str) are
    invoked via call_soon_threadsafe as chunks arrive.
    """
    payload = {
        "model": MODEL,
        "modalities": ["text", "audio"],
        "audio": {"voice": VOICE, "format": "pcm16"},
        "stream": True,
        "messages": messages,
        "tools": TOOLS,
    }
    import urllib.request

    tool_calls: list[dict] = []
    usage = None

    def worker():
        nonlocal usage
        try:
            req = urllib.request.Request(
                API, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                parts_args: dict[str, list] = {}
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    sdata = line[5:].strip()
                    if sdata == "[DONE]":
                        break
                    try:
                        ev = json.loads(sdata)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("error"):
                        print(f"[api error] {json.dumps(ev['error'])[:300]}",
                              flush=True)
                    if ev.get("usage"):
                        usage = ev["usage"]
                    ch = (ev.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    ad = (delta.get("audio") or {}).get("data")
                    if ad:
                        loop.call_soon_threadsafe(on_audio, ad)
                    tr = (delta.get("audio") or {}).get("transcript")
                    if tr:
                        loop.call_soon_threadsafe(on_text, tr)
                    for i, tc in enumerate(delta.get("tool_calls") or []):
                        fn = tc.get("function") or {}
                        slot = parts_args.setdefault(i, {"name": None, "args": []})
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"].append(fn["arguments"])
                tool_calls.extend(
                    {"name": v["name"], "arguments": "".join(v["args"])}
                    for _, v in sorted(parts_args.items()))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"[api error] HTTP {e.code}: {body}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[error] stream failed: {e!r}", flush=True)

    await asyncio.to_thread(worker)
    return tool_calls, usage


async def run():
    global loop, mic_q, turn_seq
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue(maxsize=400)
    speaker.start()

    dev = sd.query_devices(int(os.environ["CHAT_INPUT_DEVICE"])
                           if os.getenv("CHAT_INPUT_DEVICE")
                           else sd.default.device[0])
    rate = int(dev["default_samplerate"])
    ch = 1
    mic = sd.RawInputStream(samplerate=rate, blocksize=int(rate / 10),
                            dtype="int16", channels=ch, device=dev["index"],
                            callback=mic_callback)
    mic.start()

    resampler = create_stream_resampler()
    vad_resampler = create_stream_resampler()
    vad = SileroVADAnalyzer(
        sample_rate=IN_RATE,
        params=VADParams(min_volume=float(os.getenv("CHAT_MIN_VOLUME", "0.15")),
                         start_secs=0.2, stop_secs=0.4))
    vad.set_sample_rate(IN_RATE)

    history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    total_cost = 0.0
    turns = 0

    print(f"Friday (gpt-audio-mini, hand-built turns) listening — speak, then "
          f"pause. Ctrl+C to quit.\n[device] {dev['name']} @ {rate} Hz\n"
          f"[key] OPENROUTER_API_KEY {'set' if KEY else 'MISSING'}\n> ",
          flush=True)

    state = "quiet"
    lvl_buf = bytearray()
    utter24 = bytearray()      # 24 kHz utterance being recorded
    utt_start = None
    quiet_since = None
    last_level_print = time.monotonic()

    async def do_turn(utt: bytes):
        """Send one utterance, stream + play the reply, run tools."""
        nonlocal total_cost, turns
        t_end = time.perf_counter()
        b64 = pcm_to_wav_b64(utt)
        turns += 1
        print(f"[vad] speech stopped ({len(utt)/2/24000:.1f}s) — asking "
              f"{MODEL}...", flush=True)

        my_turn = turn_seq
        first_audio: dict = {}

        def on_audio(b64_chunk):
            if my_turn != turn_seq:
                return  # stale audio from an interrupted turn
            first_audio.setdefault("t", time.perf_counter() - t_end)
            asyncio.ensure_future(speaker.feed(base64.b64decode(b64_chunk)))

        text_parts: list[str] = []

        def on_text(t):
            text_parts.append(t)

        msgs = history + [{"role": "user", "content": [
            {"type": "text", "text": "Answer my spoken question."},
            {"type": "input_audio",
             "input_audio": {"data": b64, "format": "wav"}},
        ]}]

        tool_calls, usage = await chat_stream(msgs, on_audio, on_text)
        transcript = "".join(text_parts).strip()

        # follow-up when the model called a tool. NOTE: gpt-audio-mini via
        # OpenRouter rejects proper tool_calls/tool messages after audio input
        # ("This model does not support the format you provided") — verified.
        # The result must be embedded as a plain-string user message.
        if any(tc["name"] for tc in tool_calls):
            name = next(tc["name"] for tc in tool_calls if tc["name"])
            args = next((tc["arguments"] for tc in tool_calls if tc["name"]), "{}")
            tool_result = json.dumps({"recent_meeting": "M7 POC scoping",
                                      "next_event": "Friday demo review"})
            follow = msgs + [{"role": "user", "content": [
                {"type": "text",
                 "text": f"{name} returned: {tool_result}. Answer the "
                         f"original question briefly."}]}]
            text_parts.clear()
            first_audio.clear()
            _, usage2 = await chat_stream(follow, on_audio, on_text)
            if usage2:
                usage = usage2
            transcript = "".join(text_parts).strip()

        # memory: approximate — assistant reply stored as its transcript text
        history.append({"role": "user", "content": [
            {"type": "text", "text": "Answer my spoken question."},
            {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        ]})
        history.append({"role": "assistant",
                        "content": transcript or "(tool result answer)"})

        cost = (usage or {}).get("cost") or 0
        total_cost += cost
        lat = first_audio.get("t") or 0.0
        print(
            f"\n--- turn {turns} --- latency {lat:.2f}s | cost ~${cost:.4f} | "
            f"total ~${total_cost:.4f}\nFriday: {transcript}\n> ", flush=True)

    try:
        while True:
            block = await mic_q.get()  # 100 ms @ device rate
            pcm = np.frombuffer(block, dtype=np.int16)
            raw16 = await vad_resampler.resample(block, rate, IN_RATE)
            raw24 = await resampler.resample(block, rate, 24000)

            lvl_buf += raw24
            del lvl_buf[: max(0, len(lvl_buf) - int(24000 * 0.5) * 2)]
            if time.monotonic() - last_level_print > 5:
                last_level_print = time.monotonic()
                try:
                    vol = calculate_audio_volume(bytes(lvl_buf), 24000)
                except Exception:
                    vol = -1
                print(f"[mic] listening… level={vol:.2f}", flush=True)

            st = await vad.analyze_audio(raw16)

            # barge-in: user speech while Friday plays -> cut playback
            if st == VADState.SPEAKING and speaker.playing:
                turn_seq += 1
                speaker.barge_in()
                print("[barge-in] cutting playback", flush=True)
                state = "recording"
                utter24.clear()
                utt_start = time.monotonic()
                quiet_since = None
                continue

            if st in (VADState.SPEAKING, VADState.STARTING):
                if state == "quiet":
                    state = "recording"
                    utter24.clear()
                    utt_start = time.monotonic()
                    print("[vad] speech started", flush=True)
                utter24 += raw24
                quiet_since = None
            elif st == VADState.QUIET and state == "recording":
                utter24 += raw24
                if quiet_since is None:
                    quiet_since = time.monotonic()
                dur = len(utter24) / 2 / 24000
                if (time.monotonic() - quiet_since >= 0.5
                        and dur >= 0.4):
                    state = "quiet"
                    utt = bytes(utter24)
                    utter24.clear()
                    if speaker.playing:
                        continue  # playback still ongoing; don't double-send
                    asyncio.ensure_future(do_turn(utt))
                elif dur < 0.4 and time.monotonic() - quiet_since > 1.0:
                    state = "quiet"
                    utter24.clear()
    except asyncio.CancelledError:
        pass
    finally:
        mic.stop()
        mic.close()
        speaker.stop()


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    def _hup(*_):
        os._exit(0)
    signal.signal(signal.SIGHUP, _hup)
    main()