#!/usr/bin/env python3
"""M7 POC — interactive CLI on the RAW REALTIME websocket (no Pipecat).

Same realtime model as ../pipecat-poc/chat.py, but the conversation layer is
HAND-BUILT here on a bare websocket (that is this POC's point — contrast with
pipecat where the framework provides it):

  mic (sounddevice, 100 ms blocks) -> input_audio_buffer.append
    -> Silero VAD (local ONNX) end-of-utterance  [hand-built]
    -> input_audio_buffer.commit + response.create  [hand-built — the server
       NEVER auto-commits on this model, verified in probe.py]
    -> streaming audio deltas played through speakers
    -> tool results via conversation.item.create(function_call_output)
    -> conversation memory: the realtime session holds server-side state

Requires OPENAI_API_KEY in this folder's .env (same key as probe.py).
Headphones recommended — no echo cancellation.

Run: ./run-chat-realtime.sh   (Ctrl+C or closing the terminal exits)
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
import websockets

# this CLI only borrows pipecat's audio libraries (from pipecat-poc's
# venv); silence the library's loguru banners BEFORE the first pipecat
# import, so it doesn't look like the pipecat-framework experiment runs
from loguru import logger
logger.remove()
from pipecat.audio.utils import calculate_audio_volume, create_stream_resampler
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

HERE = Path(__file__).parent
IN_RATE = 16000        # VAD native rate
MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "alloy")
WS_URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"
OUT_RATE = 44100

# OpenAI-direct realtime pricing (audio), per 1M tokens
AUDIO_IN_USD_PER_M = 10.0
AUDIO_OUT_USD_PER_M = 20.0
# measured densities: ~10 tok/s audio in, ~15 tok/s audio out
TOK_PER_SEC_IN = 10.0
TOK_PER_SEC_OUT = 15.0


def load_key() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("OPENAI_API_KEY not set (put it in experiments/realtime-model-poc/.env)")


SYSTEM_PROMPT = (
    "You are Friday, a virtual worker. Keep spoken answers brief (2-3 "
    "sentences). Call get_meeting_context when the question touches "
    "meetings, notes, or the calendar."
)

SESSION_UPDATE = {
    "type": "session.update",
    "session": {
        "type": "realtime",
        "instructions": SYSTEM_PROMPT,
        "audio": {
            "input": {
                # Manual turns: the server NEVER auto-commits on this model
                # (verified raw, 4 configurations in README) — Silero VAD
                # commits client-side below.
                "format": {"type": "audio/pcm", "rate": 24000},
                "turn_detection": None,
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": VOICE,
            },
        },
        "tools": [{
            "type": "function",
            "name": "get_meeting_context",
            "description": "Look up recent meeting notes and calendar context for Friday.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }],
    },
}


class Speaker:
    """24 kHz model audio -> device-rate playback."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.playing = False
        self.stream = None
        self.resampler = None
        self.rate = OUT_RATE

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
        outdata[:] = b"".join(chunks).ljust(frames * 2, b"\x00")
        if not chunks and self.playing and self.q.empty():
            self.playing = False  # playback finished naturally

    def start(self):
        self.resampler = create_stream_resampler()
        rate = int(sd.query_devices(kind="output")["default_samplerate"])
        self.rate = rate
        self.stream = sd.RawOutputStream(samplerate=rate, blocksize=1024,
                                         dtype="int16", channels=1,
                                         callback=self._callback)
        self.stream.start()

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
        audio = pcm24
        if self.rate != 24000:
            audio = await self.resampler.resample(pcm24, 24000, self.rate)
        if audio:
            self.q.put(audio)
            self.playing = True


speaker = Speaker()
ws_send = None            # async send callable
loop = None
mic_q = None
turn_seq = 0


def mic_callback(indata, frames, t, status):
    if loop is not None and mic_q is not None:
        loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))


async def send(ev: dict):
    await ws_send(json.dumps(ev))


async def recv_loop(ws, metrics):
    """Handles server events: audio deltas, transcripts, tools, usage."""
    n = 0
    try:
        await _recv_inner(ws, metrics)
        print("[recv] connection closed cleanly (async-for ended)", flush=True)
    except Exception as e:
        import traceback
        print(f"[recv error] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()


async def _recv_inner(ws, metrics):
    n = 0
    call_id = None
    tool_args: list[str] = []
    tool_name = None
    print("[recv] listening", flush=True)
    while True:
        raw = await ws.recv()
        n += 1
        ev = json.loads(raw)
        et = ev.get("type")
        if os.getenv("CHAT_DEBUG") and et not in (
                "response.output_audio.delta",):
            print(f"[rt] {et}", flush=True)
        if et == "response.created":
            metrics.active = True
        if et == "response.output_audio.delta":
            metrics.first_audio.setdefault(
                "t", round(time.perf_counter() - metrics.t_commit, 2))
            await speaker.feed(base64.b64decode(ev["delta"]))
        elif et == "response.output_audio_transcript.delta":
            metrics.text.append(ev["delta"])
        elif et == "response.function_call_arguments.delta":
            tool_args.append(ev["delta"])
        elif et == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                tool_name = item.get("name")
        elif et in ("response.done", "response.cancelled", "response.failed"):
            metrics.active = False
            u = (ev.get("response") or {}).get("usage") or {}
            in_a = (u.get("input_token_details") or {}).get("audio_tokens", 0)
            out_a = (u.get("output_token_details") or {}).get("audio_tokens", 0)
            metrics.cost = in_a * AUDIO_IN_USD_PER_M / 1e6 + out_a * AUDIO_OUT_USD_PER_M / 1e6
            metrics.in_audio_secs = in_a / TOK_PER_SEC_IN if TOK_PER_SEC_IN else 0
            if call_id and tool_name:
                # feed the tool result back into the live session
                result = json.dumps({"recent_meeting": "M7 POC scoping",
                                     "next_event": "Friday demo review"})
                await send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": call_id,
                    "output": result}})
                await send({"type": "response.create"})
                call_id, tool_name, tool_args = None, None, []
        elif et in ("error", "invalid_request_error"):
            if ev.get("error", {}).get("code") == "response_cancel_not_active":
                continue  # harmless race: cancel vs. just-completed response
            print(f"[api error] {json.dumps(ev)[:300]}", flush=True)


ws_recv = None


async def run():
    global loop, mic_q, ws_send, ws_recv, turn_seq
    key = load_key()
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue(maxsize=400)
    speaker.start()

    dev = sd.query_devices(int(os.environ["CHAT_INPUT_DEVICE"])
                           if os.getenv("CHAT_INPUT_DEVICE")
                           else sd.default.device[0])
    rate = int(dev["default_samplerate"])
    mic = sd.RawInputStream(samplerate=rate, blocksize=int(rate / 10),
                            dtype="int16", channels=1, device=dev["index"],
                            callback=mic_callback)

    async with websockets.connect(
            WS_URL, additional_headers={"Authorization": f"Bearer {key}"},
            max_size=50 * 1024 * 1024) as ws:
      try:
        ws_send = ws.send

        # Direct recv() everywhere — no async-for/iterator state to corrupt.
        raw = await asyncio.wait_for(ws.recv(), timeout=10)   # session.created
        await send(SESSION_UPDATE)
        while True:
            raw = await ws.recv()
            if json.loads(raw).get("type") == "session.updated":
                break
        print("[rt] session ready", flush=True)

        test_wav = os.getenv("CHAT_TEST_WAV")
        mic_started = False
        if not test_wav:
            mic.start()
            mic_started = True
        resampler = create_stream_resampler()      # device -> 24k (websocket)
        vad_resampler = create_stream_resampler()  # device -> 16k (Silero)
        vad = SileroVADAnalyzer(
            sample_rate=IN_RATE,
            params=VADParams(min_volume=float(os.getenv("CHAT_MIN_VOLUME", "0.15")),
                             start_secs=0.2, stop_secs=0.4))
        vad.set_sample_rate(IN_RATE)

        class M: pass
        metrics = M()
        metrics.first_audio = {}
        metrics.text = []
        metrics.cost = 0.0
        metrics.reported = True
        metrics.t_commit = 0.0
        metrics.active = False
        recv_task = asyncio.create_task(recv_loop(ws, metrics))

        if test_wav:
            async def feeder():
                await asyncio.sleep(1.0)
                with wave.open(test_wav) as w:
                    pcm = w.readframes(w.getnframes()); wsr = w.getframerate()
                r = create_stream_resampler()
                up = await r.resample(pcm, wsr, rate)
                silence = b"\x00\x00" * int(rate * 1.5)
                for b in [up[i:i+int(rate/10)*2] for i in range(0, len(up), int(rate/10)*2)] + [silence]:
                    await mic_q.put(b)
                    await asyncio.sleep(0.1)
                print("[test] utterance fed", flush=True)
            asyncio.create_task(feeder())

        turns = 0
        total_cost = 0.0
        state = "quiet"
        quiet_since = None
        ever_sound = False
        last_level = time.monotonic()
        lvl_buf = bytearray()
        utter24 = bytearray()      # 24 kHz utterance being recorded

        print(f"Friday ({MODEL}, raw websocket, hand-built turns) listening — "
              f"speak, then pause. Ctrl+C to quit.\n[device] {dev['name']} @ "
              f"{rate} Hz\n> ", flush=True)

        async def do_turn(utt: bytes):
            """Send one utterance buffer, then commit + create (mirrors the
            audio-model POC: the mic loop stays 100% local; the websocket is
            only touched after the turn ends, never on the capture hot path)."""
            nonlocal turns
            metrics.first_audio = {}
            metrics.text = []
            metrics.cost = 0.0
            metrics.reported = False
            metrics.t_commit = time.perf_counter()
            turns += 1
            print(f"[vad] speech stopped ({len(utt)/2/24000:.1f}s) — sending "
                  f"turn {turns}...", flush=True)
            chunk = 24000 * 2          # 1 s of 24 kHz PCM16 per append
            try:
                for i in range(0, len(utt), chunk):
                    await send({"type": "input_audio_buffer.append",
                                "audio": base64.b64encode(utt[i:i+chunk]).decode()})
                await send({"type": "input_audio_buffer.commit"})
                await send({"type": "response.create"})
            except Exception as e:
                import traceback
                print(f"[do_turn error] {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()

        while True:
            # timeout tick: the commit check must run even when no mic
            # blocks arrive (silence after an utterance still needs
            # evaluating, or the turn is never committed)
            try:
                block = await asyncio.wait_for(mic_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                block = None

            if block is None:
                if state == "recording":
                    # keep the VAD advancing on silence so the analyzer
                    # can reach QUIET (stop_secs needs samples, not time)
                    st = await vad.analyze_audio(
                        b"\x00\x00" * int(IN_RATE / 10 * 2))
                    if st == VADState.QUIET and quiet_since is None:
                        quiet_since = time.monotonic()
                    dur = len(utter24) / 2 / 24000
                    if (quiet_since is not None
                            and time.monotonic() - quiet_since >= 0.5
                            and dur >= 0.4):
                        state = "quiet"
                        utt = bytes(utter24)
                        utter24.clear()
                        asyncio.ensure_future(do_turn(utt))
                    elif (dur < 0.4 and quiet_since is not None
                            and time.monotonic() - quiet_since > 1.0):
                        state = "quiet"
                        utter24.clear()
                # report a finished turn once its audio finished playing
                if (state == "quiet" and not speaker.playing
                        and metrics.first_audio.get("t")
                        and not metrics.reported):
                    lat = metrics.first_audio.get("t", 0.0)
                    total_cost += metrics.cost
                    metrics.reported = True
                    print(
                        f"\n--- turn {turns} --- latency {lat:.2f}s | "
                        f"cost ~${metrics.cost:.4f} | "
                        f"total ~${total_cost:.4f}\n"
                        f"Friday: {''.join(metrics.text).strip()}\n> ",
                        flush=True)
                continue

            pcm = np.frombuffer(block, dtype=np.int16)
            raw24 = await resampler.resample(block, rate, 24000)
            raw16 = await vad_resampler.resample(block, rate, IN_RATE)

            lvl_buf += raw24
            del lvl_buf[: max(0, len(lvl_buf) - int(24000 * 0.5) * 2)]
            if time.monotonic() - last_level > 5:
                last_level = time.monotonic()
                try:
                    vol = calculate_audio_volume(bytes(lvl_buf), 24000)
                except Exception:
                    vol = -1
                print(f"[mic] listening… level={vol:.2f}", flush=True)
                if lvl_buf:
                    a = np.frombuffer(bytes(lvl_buf), np.int16).astype(np.float64)
                    rms = float(np.sqrt(np.mean(a ** 2)))
                    if rms > 300:
                        ever_sound = True
                    elif not ever_sound:
                        print("[warn] mic capture is silent — the machine is "
                              "not receiving your voice. On RDP: enable "
                              "microphone redirection and reconnect.",
                              flush=True)

            st = await vad.analyze_audio(raw16)

            # barge-in: user speech while Friday plays -> cut playback
            if st == VADState.SPEAKING and speaker.playing:
                turn_seq += 1
                speaker.barge_in()
                utter24.clear()
                await send({"type": "input_audio_buffer.clear"})
                if getattr(metrics, "active", False):
                    await send({"type": "response.cancel"})
                print("[barge-in] cutting playback", flush=True)
                state = "recording"
                quiet_since = None
                continue

            if st in (VADState.SPEAKING, VADState.STARTING):
                if state == "quiet":
                    state = "recording"
                    utter24.clear()
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
                elif (dur < 0.4
                        and time.monotonic() - quiet_since > 1.0):
                    state = "quiet"
                    utter24.clear()

            # report a finished turn once its audio finished playing
            if (state == "quiet" and not speaker.playing
                    and metrics.first_audio.get("t")
                    and not metrics.reported):
                lat = metrics.first_audio.get("t", 0.0)
                total_cost += metrics.cost
                metrics.reported = True
                print(
                    f"\n--- turn {turns} --- latency {lat:.2f}s | "
                    f"cost ~${metrics.cost:.4f} | "
                    f"total ~${total_cost:.4f}\n"
                    f"Friday: {''.join(metrics.text).strip()}\n> ",
                    flush=True)
      except asyncio.CancelledError:
        print("[exit] main loop cancelled", flush=True)
      except BaseException as e:
        import traceback
        print(f"[body] exited with {type(e).__name__}: {e!r}", flush=True)
        traceback.print_exc()
        raise
      finally:
        recv_task.cancel()
        if mic_started:
            mic.stop()
            mic.close()
        speaker.stop()


def main():
    try:
        asyncio.run(run())
        print("[main] run() returned normally", flush=True)
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        import traceback
        print(f"[main] run() ended with {type(e).__name__}: {e!r}", flush=True)
        traceback.print_exc()


if __name__ == "__main__":
    def _hup(*_):
        os._exit(0)
    signal.signal(signal.SIGHUP, _hup)
    main()
