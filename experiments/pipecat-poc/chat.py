#!/usr/bin/env python3
"""M7 POC — interactive CLI: talk to Friday through mic and speakers.

Same realtime model + Pipecat pipeline as probe.py, plus:
- sounddevice mic input (16 kHz → service resamples to 24 kHz internally)
- Silero VAD (local ONNX) for end-of-turn detection and barge-in
- LocalAudioTransport equivalent built on sounddevice (no pyaudio/system deps)
- per-turn latency and cost estimate printed after every exchange

Conversation memory is free: the realtime session keeps server-side state.

Headphones recommended (no echo cancellation — speaker bleed triggers barge-in).

Run: uv run --project experiments/pipecat-poc experiments/pipecat-poc/chat.py
Ctrl+C to quit.
"""

import asyncio
import json
import os
import queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
import wave
from loguru import logger

from pipecat.audio.utils import calculate_audio_volume, create_stream_resampler
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.realtime import events as rt_events
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

HERE = Path(__file__).parent

IN_RATE = 16000     # Silero VAD native rate; service resamples to 24k itself
IN_BLOCK = 512      # Silero expects 512-sample chunks @ 16 kHz
OUT_RATE = 44100    # typical output device rate; resampled from 24 k TTS audio

# OpenAI-direct realtime audio pricing (per 1M audio tokens)
AUDIO_IN_USD_PER_M = 10.0
AUDIO_OUT_USD_PER_M = 20.0
# measured density: ~10 tok/s audio in, ~15 tok/s audio out
TOK_PER_SEC_IN = 10.0
TOK_PER_SEC_OUT = 15.0


def load_env() -> dict:
    cfg = {}
    for line in (HERE / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    if not cfg.get("OPENAI_API_KEY"):
        sys.exit("Missing OPENAI_API_KEY in experiments/pipecat-poc/.env")
    return {
        "api_key": cfg["OPENAI_API_KEY"],
                "model": (os.environ.get("CHAT_MODEL")
                  or cfg.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")),
    }


class MicInput(FrameProcessor):
    """sounddevice mic → 16 kHz InputAudioRawFrame."""

    def __init__(self):
        super().__init__()
        # NOTE: the reader MUST yield to the event loop — a blocking
        # queue.Queue.get here starved the loop completely and downstream
        # processors never ran (root cause of the "CLI never responds" bug).
        self._q: asyncio.Queue[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._task = None
        self._resampler = None
        self._running = False
        self._device_rate = None
        self._debug = bool(os.getenv("CHAT_DEBUG"))
        MicInput._vol_err_shown = False
        MicInput.frames_pushed = 0

    def _callback(self, indata, frames, t, status):
        # PortAudio thread — hand the block to the event loop thread-safely.
        if status and self._debug:
            print(f"[mic status] {status}", flush=True)
        if self._loop and self._q is not None:
            self._loop.call_soon_threadsafe(self._q.put_nowait, bytes(indata))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._loop = asyncio.get_running_loop()
            self._q = asyncio.Queue(maxsize=200)
            self._resampler = create_stream_resampler()
            dev = sd.query_devices(kind="input")
            self._device_rate = int(dev["default_samplerate"])
            if self._debug:
                print(f"[mic] opening '{dev['name']}' at {self._device_rate} Hz", flush=True)
            if os.getenv("CHAT_TEST_WAV"):
                print("[test] feeder mode — wav replaces mic", flush=True)
                async def _feeder():
                    try:
                        await asyncio.sleep(1.0)
                        with wave.open(os.environ["CHAT_TEST_WAV"]) as w:
                            pcm = w.readframes(w.getnframes()); wsr = w.getframerate()
                        r = create_stream_resampler()
                        up = await r.resample(pcm, wsr, self._device_rate)
                        chunk = int(self._device_rate / 10) * 2
                        silence = b"\x00\x00" * int(self._device_rate * 1.5)
                        for b in [up[i:i+chunk] for i in range(0, len(up), chunk)] + [silence]:
                            if self._q:
                                self._q.put_nowait(b)
                            await asyncio.sleep(0.1)
                        print("[test] utterance fed", flush=True)
                    except Exception as e:
                        import traceback
                        print(f"[test error] {type(e).__name__}: {e}", flush=True)
                        traceback.print_exc()
            else:
                self._stream = sd.RawInputStream(
                    samplerate=self._device_rate,
                    blocksize=int(self._device_rate / 10),  # 100 ms blocks — batching matters:
                    # per-10ms SOXR VHQ resampling starves the event loop and the
                    # downstream pipeline never processes frames (verified)
                    dtype="int16",
                    channels=1,
                    device=int(os.environ["CHAT_INPUT_DEVICE"])
                    if os.getenv("CHAT_INPUT_DEVICE")
                    else None,
                    callback=self._callback,
                )
                self._stream.start()
            self._running = True
            self._task = self.create_task(self._reader())
            if os.getenv("CHAT_TEST_WAV"):
                self.create_task(_feeder())
        await self.push_frame(frame, direction)

    async def _reader(self):
        last_dbg = time.monotonic()
        dbg_buf = bytearray()
        while self._running:
            block = await self._q.get()  # real await — yields to the loop
            pcm = np.frombuffer(block, dtype=np.int16)
            dbg_buf += block
            del dbg_buf[: max(0, len(dbg_buf) - int(self._device_rate * 0.5 * 2))]
            level_every = 2.0 if self._debug else 5.0
            if time.monotonic() - last_dbg > level_every:
                rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
                try:
                    vol = calculate_audio_volume(bytes(dbg_buf), self._device_rate)
                except Exception as e:
                    vol = -1
                    if self._debug and not MicInput._vol_err_shown:
                        MicInput._vol_err_shown = True
                        print(f"[mic] loudness calc failed: {type(e).__name__}: {e}", flush=True)
                if self._debug:
                    print(f"[mic] rms={rms:.0f} loudness={vol:.2f}", flush=True)
                else:
                    print(
                        f"[mic] listening… level={vol:.2f} frames_pushed={MicInput.frames_pushed}",
                        flush=True,
                    )
                last_dbg = time.monotonic()
            if self._device_rate != IN_RATE:
                audio = await self._resampler.resample(pcm.tobytes(), self._device_rate, IN_RATE)
            else:
                audio = pcm.tobytes()
            MicInput.frames_pushed += 1
            await self.push_frame(
                InputAudioRawFrame(audio=audio, sample_rate=IN_RATE, num_channels=1)
            )

    async def stop(self, frame: EndFrame):
        self._shutdown()
        await super().stop(frame)
        await self.push_frame(frame)

    async def cancel(self, frame: CancelFrame):
        self._shutdown()
        await super().cancel(frame)

    def _shutdown(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class SpeakerOutput(FrameProcessor):
    """TTSAudioRawFrame (24 kHz) → resampled 44.1 kHz playback via sounddevice."""

    def __init__(self):
        super().__init__()
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stream = None
        self._resampler = None
        self._playing = False

    def _callback(self, outdata, frames, t, status):
        # Fill outdata from queued audio; tolerate odd-length chunks and
        # underruns (zeros) rather than raising inside the PortAudio callback.
        need = frames * 2  # bytes (int16 mono)
        chunks = []
        while need > 0:
            try:
                d = self._q.get_nowait()
            except queue.Empty:
                break
            d = d[: len(d) // 2 * 2]
            take = min(need, len(d))
            chunks.append(d[:take])
            need -= take
            if len(d) > take:
                self._q.queue.appendleft(d[take:])
        data = b"".join(chunks)
        outdata[:] = data.ljust(frames * 2, b"\x00")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._resampler = create_stream_resampler()
            self._stream = sd.RawOutputStream(
                samplerate=OUT_RATE,
                blocksize=1024,
                dtype="int16",
                channels=1,
                callback=self._callback,
            )
            self._stream.start()
        elif isinstance(frame, TTSAudioRawFrame):
            if frame.sample_rate != OUT_RATE:
                audio = await self._resampler.resample(frame.audio, frame.sample_rate, OUT_RATE)
            else:
                audio = frame.audio
            self._q.put(audio)
            self._playing = True
        elif isinstance(frame, InterruptionFrame):
            # barge-in: drop queued audio immediately
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._playing = False
        await self.push_frame(frame, direction)

    async def cleanup(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        await super().cleanup()


class TurnMetrics(FrameProcessor):
    """Prints per-turn latency, transcript, and cost estimate."""

    def __init__(self):
        super().__init__()
        self.t_turn_end: float | None = None
        self.first_audio_latency: float | None = None
        self.text: list[str] = []
        self.audio_secs_out = 0.0
        self.turns = 0
        self.total_cost_usd = 0.0
        self._t0 = time.perf_counter()
        self._in_secs = 0.0

    def _now(self) -> float:
        return time.perf_counter() - self._t0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, InputAudioRawFrame):
            self._in_secs += len(frame.audio) / 2 / frame.sample_rate
        elif isinstance(frame, UserStartedSpeakingFrame):
            print("[vad] speech started", flush=True)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            print("[vad] speech stopped — asking Friday...", flush=True)
            self.t_turn_end = time.perf_counter()
            self.first_audio_latency = None
            self.text = []
        elif isinstance(frame, TTSAudioRawFrame):
            if self.t_turn_end and self.first_audio_latency is None:
                self.first_audio_latency = time.perf_counter() - self.t_turn_end
            self.audio_secs_out += len(frame.audio) / 2 / frame.sample_rate
        elif isinstance(frame, TTSTextFrame):
            self.text.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.turns += 1
            in_tok = self._in_secs * TOK_PER_SEC_IN
            out_tok = self.audio_secs_out * TOK_PER_SEC_OUT
            turn_cost = (in_tok * AUDIO_IN_USD_PER_M + out_tok * AUDIO_OUT_USD_PER_M) / 1e6
            self.total_cost_usd += turn_cost
            print(
                f"\n--- turn {self.turns} --- "
                f"latency {self.first_audio_latency:.2f}s | "
                f"cost ~${turn_cost:.4f} | total ~${self.total_cost_usd:.4f}\n"
                f"Friday: {''.join(self.text).strip()}\n> ",
                flush=True,
            )
            self._in_secs = 0.0
            self.audio_secs_out = 0.0
        elif isinstance(frame, ErrorFrame):
            print(f"\n[error] {frame}", flush=True)
        await self.push_frame(frame, direction)


class Spy(FrameProcessor):
    """Debug spy: counts frames by type, prints a summary every 5 s."""

    def __init__(self, label):
        super().__init__()
        self.label = label
        self.counts: dict = {}
        self.t_last = time.perf_counter()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        n = type(frame).__name__
        if n in ("InputAudioRawFrame", "StartFrame", "EndFrame"):
            self.counts[n] = self.counts.get(n, 0) + 1
        else:
            self.counts[n] = self.counts.get(n, 0) + 1
            if os.getenv("CHAT_SPY_VERBOSE"):
                print(f"[{self.label}] {n}", flush=True)
        if time.perf_counter() - self.t_last > 5:
            self.t_last = time.perf_counter()
            top = {k: v for k, v in sorted(self.counts.items(), key=lambda x: -x[1])[:6]}
            print(f"[spy:{self.label}] {top}", flush=True)
        await self.push_frame(frame, direction)


class SDTransport(BaseTransport):
    def __init__(self, metrics: TurnMetrics):
        super().__init__()
        self._mic = MicInput()
        self._metrics = metrics
        self._spk = SpeakerOutput()

    def input(self) -> FrameProcessor:
        return self._mic

    def output(self) -> FrameProcessor:
        return self._metrics


async def run() -> dict:
    cfg = load_env()
    metrics = TurnMetrics()

    async def get_meeting_context(params: FunctionCallParams):
        await params.result(
            json.dumps(
                {"recent_meeting": "M7 POC scoping", "next_event": "Friday demo review"}
            )
        )

    service = OpenAIRealtimeLLMService(
        api_key=cfg["api_key"],
        settings=OpenAIRealtimeLLMService.Settings(
            model=cfg["model"],
            system_instruction=(
                "You are Friday, a virtual worker. Keep spoken answers brief "
                "(2-3 sentences). Call get_meeting_context when the question "
                "touches meetings, notes, or the calendar."
            ),
            session_properties=rt_events.SessionProperties(
                audio=rt_events.AudioConfiguration(
                    # OpenAI's server VAD never auto-commits on this model —
                    # turns are driven client-side by Silero VAD (see probe
                    # findings in README).
                    input=rt_events.AudioInput(
                        format=rt_events.PCMAudioFormat(rate=24000),
                        turn_detection=False,
                    ),
                    output=rt_events.AudioOutput(
                        format=rt_events.PCMAudioFormat(rate=24000),
                        voice="alloy",
                    ),
                ),
                tools=[{
                    "type": "function",
                    "name": "get_meeting_context",
                    "description": "Look up recent meeting notes and calendar context for Friday.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }],
            ),
        ),
    )
    service.register_function("get_meeting_context", get_meeting_context)

    pair = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    min_volume=float(os.getenv("CHAT_MIN_VOLUME", "0.15")),
                    start_secs=0.2,
                    stop_secs=0.3,
                )
            )
        ),
        realtime_service_mode=True,
    )

    transport = SDTransport(metrics)
    debug = bool(os.getenv("CHAT_DEBUG"))
    parts = [transport.input()]
    if debug:
        spy_in = Spy("in")
        spy_out = Spy("after-aggregator")
        parts += [spy_in, pair.user(), spy_out]
    else:
        parts += [pair.user()]
    parts += [service, pair.assistant(), transport.output(), transport._spk]
    worker = PipelineWorker(
        Pipeline(parts),
        params=PipelineParams(allow_interruptions=True),
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    # Ctrl+C: the runner installs its own SIGINT handler when run() starts,
    # which waits for a graceful end that can hang on an active realtime
    # session. Re-install ours AFTER it starts (0.5 s later): cancel the
    # runner, and hard-exit after 3 s if shutdown stalls.
    loop = asyncio.get_running_loop()
    import signal as _signal

    def _on_sigint():
        print("\n[closing] goodbye", flush=True)
        asyncio.create_task(runner.cancel())
        loop.call_later(3.0, lambda: os._exit(0))

    def _on_hup():
        # terminal closed — exit immediately, never linger in the background
        os._exit(0)

    async def _install_signals():
        await asyncio.sleep(0.5)  # after the runner installs its own handlers
        loop.add_signal_handler(_signal.SIGINT, _on_sigint)
        loop.add_signal_handler(_signal.SIGHUP, _on_hup)

    asyncio.create_task(_install_signals())

    print(
        "Friday listening — speak, then pause. Ctrl+C to quit.\n"
        "(Headphones recommended: no echo cancellation.)\n> ",
        flush=True,
    )
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    return {
        "turns": metrics.turns,
        "total_cost_usd": round(metrics.total_cost_usd, 4),
        "wall_total_s": round(time.perf_counter() - metrics._t0, 1),
    }


async def main():
    logger.remove()
    if os.getenv("PIPECAT_DEBUG"):
        import sys
        logger.add(sys.stderr, level="DEBUG")
    result = await run()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = HERE / "results" / f"chat-{stamp}.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"\nsession summary: {json.dumps(result)} -> {out}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")