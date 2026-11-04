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
logger.remove()  # kill pipecat's import-time banner before it loads

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
from cli_ui import banner, dim, err, friday, hdr, tool, warn as _warn_c, you as _you_c
import token_accounting as ta
from poc_report import SessionReport, load_environment, endpoint_silence
from poc_pricing import usage_cost
from poc_fixtures import fixture_plan, feed_plan
from poc_pipeline import SessionBoundary, realtime_user_params
load_environment(HERE)
from pipecat.frames.frames import MetricsFrame
from pipecat.metrics.metrics import LLMTokenUsage

_whisper = None  # lazy-loaded local whisper (user-speech display)
TOKENS = ta.TokenAccount()  # module-level: run() feeds it, main() reports it
WORKDIR = os.environ.get("CHAT_WORKDIR") or os.getcwd()

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
    import configparser
    presets = configparser.ConfigParser()
    presets.read(HERE / "models.ini")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is required")
    return {"api_key": key, "model": os.environ.get("CHAT_MODEL")
            or presets.get("realtime", "model")}



class MicInput(FrameProcessor):
    """sounddevice mic → 16 kHz InputAudioRawFrame."""

    def __init__(self, session=None):
        super().__init__()
        self.session = session
        self.fixture_stop = None
        self.fixture_ready = lambda: True
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
            plan = fixture_plan()
            if plan:
                self._device_rate = IN_RATE
            else:
                dev = sd.query_devices(int(os.environ["CHAT_INPUT_DEVICE"])
                                       if os.getenv("CHAT_INPUT_DEVICE") else None, kind="input")
                self._device_rate = int(dev["default_samplerate"])
                self._stream = sd.RawInputStream(
                    samplerate=self._device_rate, blocksize=int(self._device_rate/10),
                    dtype="int16", channels=1, device=dev["index"], callback=self._callback)
                self._stream.start()
            self._running = True
            self._task = self.create_task(self._reader())
            if plan:
                self.create_task(feed_plan(plan, self.session, self._q.put_nowait,
                    self._device_rate, self.fixture_playing, self.fixture_stop,
                    ready=self.fixture_ready))
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
                    print(dim(f"[mic] rms={rms:.0f} loudness={vol:.2f}"), flush=True)
                last_dbg = time.monotonic()
            if self._device_rate != IN_RATE:
                audio = await self._resampler.resample(pcm.tobytes(), self._device_rate, IN_RATE)
            else:
                audio = pcm.tobytes()
            if self.session is not None:
                self.session.observe_input(audio, IN_RATE)
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

    def __init__(self, session=None):
        super().__init__()
        self.session = session
        self._last_feed = 0.0
        self._q: queue.Queue[bytes] = queue.Queue()
        self._stream = None
        self._resampler = None
        self._playing = False

    @property
    def playing(self):
        return not self._q.empty() or time.monotonic()-self._last_feed < .3

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
        if data and self.session is not None:
            self.session.playback(data, OUT_RATE,
                delay_s=float(t.outputBufferDacTime-t.currentTime))

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
            if self.session is not None:
                self.session.audio_received(frame.audio, frame.sample_rate)
            if frame.sample_rate != OUT_RATE:
                audio = await self._resampler.resample(frame.audio, frame.sample_rate, OUT_RATE)
            else:
                audio = frame.audio
            self._q.put(audio)
            self._last_feed = time.monotonic()
            self._playing = True
        elif isinstance(frame, InterruptionFrame):
            # barge-in: drop queued audio immediately
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._playing = False
            self._last_feed = 0.0
        await self.push_frame(frame, direction)

    async def cleanup(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        await super().cleanup()


class TurnMetrics(FrameProcessor):
    """Per-turn latency, transcript, and real token accounting. The
    realtime service emits MetricsFrame(LLMTokenUsage) at each
    response.done — that is the actual billed usage."""

    def __init__(self, tokens: ta.TokenAccount, session=None):
        super().__init__()
        self.session = session
        self.tokens = tokens
        self.t_turn_end: float | None = None
        self.first_audio_latency: float | None = None
        self.text: list[str] = []
        self.audio_secs_out = 0.0
        self.turns = 0
        self.total_cost_usd = 0.0
        self._t0 = time.perf_counter()
        self._in_secs = 0.0
        self._utt = bytearray()      # mic audio of the current user utterance
        self._utt_rate = 16000
        self._recording = False      # buffering only while user speaks
        self._you_task = None        # pending "You:" transcription
        self._debug = bool(os.getenv("CHAT_DEBUG"))

    def _now(self) -> float:
        return time.perf_counter() - self._t0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, MetricsFrame):
            for m in frame.data:
                if isinstance(m, LLMTokenUsage):
                    self.tokens.add(m)
        if isinstance(frame, InputAudioRawFrame):
            self._in_secs += len(frame.audio) / 2 / frame.sample_rate
            # buffer the live utterance so we can show what the agent hears
            if self._recording and len(self._utt) < 16000 * 30 * 2:
                self._utt += frame.audio
                self._utt_rate = frame.sample_rate
        elif isinstance(frame, UserStartedSpeakingFrame):
            if self._debug:
                print(dim("[vad] speech started"), flush=True)
            if self._you_task is not None:   # flush an unreported utterance
                await self.flush_you()
            if any(self.tokens.cur[k] for k in ("in", "out")):
                self.tokens.end_turn()   # close the previous turn's bucket
            self._recording = True
            self._utt = bytearray()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if self._debug:
                print(dim("[vad] speech stopped"), flush=True)
            self._recording = False
            self.t_turn_end = time.perf_counter()
            self.first_audio_latency = None
            self.text = []
            self._show_you()
        elif isinstance(frame, TTSAudioRawFrame):
            if self.t_turn_end and self.first_audio_latency is None:
                self.first_audio_latency = time.perf_counter() - self.t_turn_end
            self.audio_secs_out += len(frame.audio) / 2 / frame.sample_rate
        elif isinstance(frame, ErrorFrame):
            if self.session is not None:
                self.session.error(str(frame))
            print(err(f"\n[error] {frame}"), flush=True)
        await self.push_frame(frame, direction)

    async def flush_you(self):
        """Await the pending utterance transcription and print the You line,
        so the transcript always appears before Friday's reply."""
        t = self._you_task
        self._you_task = None
        if t is None:
            return
        try:
            text = (await t).strip()
            if text:
                print(_you_c(f"You: {text}"), flush=True)
        except Exception as e:  # noqa: BLE001
            print(err(f"You: (transcription failed: {e!r})"), flush=True)

    def _show_you(self):
        """Transcribe the buffered utterance with local whisper (task stored;
        printed by flush_you before Friday's reply)."""
        utt = bytes(self._utt)
        rate = self._utt_rate
        self._utt = bytearray()
        if fixture_plan() or os.getenv("CHAT_NO_TRANSCRIPT"):
            return
        if len(utt) / 2 / rate < 0.4:
            return  # too short to be speech

        def _work() -> str:
            global _whisper
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel("base", device="cpu",
                                        compute_type="int8")
            x = (np.frombuffer(utt, dtype=np.int16).astype(np.float32)
                 / 32768.0)
            if rate != 16000:
                n = int(len(x) * 16000 / rate)
                x = np.interp(np.linspace(0, len(x) - 1, n),
                              np.arange(len(x)), x).astype(np.float32)
            segs, _info = _whisper.transcribe(x, language="en",
                                              vad_filter=True)
            return " ".join(s.text for s in segs).strip()

        async def _run():
            try:
                return await asyncio.to_thread(_work)
            except Exception as e:  # noqa: BLE001
                print(err(f"You: (transcription failed: {e!r})"), flush=True)
                return ""

        self._you_task = asyncio.ensure_future(_run())

    def report_turn(self, text: str, interrupted: bool = False):
        """Called on the assistant aggregator's on_assistant_turn_stopped
        event. (pipecat 1.15.0 consumes LLMFullResponseEndFrame and
        TTSTextFrame inside LLMAssistantAggregator, so this is the only
        reliable per-turn hook downstream — the old frame-based report
        never fired, which is why every saved session had turns: 0.)"""
        if self.session is not None and (text or interrupted):
            self.session.finish_turn(text, interrupted=interrupted)
        if interrupted:
            print(dim("— interrupted"), flush=True)
            self._in_secs = 0.0
            self.audio_secs_out = 0.0
            self.t_turn_end = None
            return
        self.turns += 1
        reply = (text or '').strip()
        tok = self.tokens.cur
        if tok["calls"]:
            tok = self.tokens.end_turn()
            total = sum(t["cost"] for t in self.tokens.turns)
            tok_s = (f"in {tok['in']:,} ({tok['in_audio']:,} audio) · "
                     f"out {tok['out']:,} ({tok['out_audio']:,} audio)")
            if tok["cached"]:
                tok_s += (f" · cached {tok['cached']:,} "
                          f"({tok['cached_text']:,} text/{tok['cached_audio']:,} audio)")
            cost = tok["cost"]
        else:
            # no usage frame yet for this turn (e.g. report fired mid-tool-
            # flow before the follow-up response's response.done) — don't
            # fabricate numbers; the session report counts everything
            tok_s = "usage pending (counted in session report)"
            cost = 0.0
            total = sum(t["cost"] for t in self.tokens.turns)
        print(friday(f"Friday: {reply}"), flush=True)
        print(dim(f"── turn {self.turns} · first audio "
                  f"{self.first_audio_latency or 0:.2f}s · {tok_s} · "
                  f"~${cost:.4f} (total ~${total:.4f})"), flush=True)
        self._in_secs = 0.0
        self.audio_secs_out = 0.0
        self.t_turn_end = None


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
        self._mic = MicInput(metrics.session)
        self._metrics = metrics
        self._spk = SpeakerOutput(metrics.session)
        self._mic.fixture_playing = lambda: self._spk.playing

    def input(self) -> FrameProcessor:
        return self._mic

    def output(self) -> FrameProcessor:
        return self._metrics


async def run() -> SessionReport:
    cfg = load_env()
    tokens = TOKENS
    session = SessionReport("realtime", "pipecat", {"realtime": cfg["model"]})
    end_silence = endpoint_silence()
    session.settings = {"vad_stop_secs": end_silence, "vad_start_secs": 0.2,
                        "turn_stop_strategy": "speech_timeout_no_transcript",
                        "additional_speech_timeout_s": 0.0}
    metrics = TurnMetrics(tokens, session)

    async def get_meeting_context(params: FunctionCallParams):
        # pipecat >= 1.x: deliver results via result_callback (params.result is gone)
        await params.result_callback(
            json.dumps(
                {"recent_meeting": "M7 POC scoping", "next_event": "Friday demo review"}
            )
        )

    async def bash(params: FunctionCallParams):
        cmd = str((params.arguments or {}).get("command", ""))
        if not cmd.strip():
            await params.result_callback(json.dumps({"returncode": -1, "output": "(empty command)"}))
            return
        loop = asyncio.get_running_loop()

        def _run():
            import subprocess
            try:
                p = subprocess.run(["bash", "-c", cmd], cwd=WORKDIR,
                                   capture_output=True, text=True, timeout=60)
                out = p.stdout or ""
                if p.stderr.strip():
                    out += ("\n[stderr] " if out else "[stderr] ") + p.stderr
                return {"returncode": p.returncode,
                        "output": (out.strip()[:4000] or "(no output)")}
            except subprocess.TimeoutExpired:
                return {"returncode": -1, "output": "(command timed out after 60s)"}
            except Exception as e:  # noqa: BLE001
                return {"returncode": -1, "output": f"(error: {e!r})"}

        result = await loop.run_in_executor(None, _run)
        print(tool(f"[tool] bash({cmd[:80]}) -> {json.dumps(result)[:160]}"), flush=True)
        await params.result_callback(json.dumps(result))

    service = OpenAIRealtimeLLMService(
        api_key=cfg["api_key"],
        settings=OpenAIRealtimeLLMService.Settings(
            model=cfg["model"],
            system_instruction=(
                "You are Friday, a virtual worker. Keep spoken answers brief "
                "(2-3 sentences). Your working directory is " + WORKDIR + " — "
                "use the bash tool to inspect and summarize the files there "
                "when asked. Call get_meeting_context when the question "
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
                }, {
                    "type": "function",
                    "name": "bash",
                    "description": "Run a bash command in the working directory and "
                                   "return its stdout/stderr. Use it to list, read, or "
                                   "summarize files in the working directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string",
                                        "description": "The bash command to run"},
                        },
                        "required": ["command"],
                    },
                }],
            ),
        ),
    )
    service.register_function("get_meeting_context", get_meeting_context)
    service.register_function("bash", bash)

    # Fixture-only diagnostic: retain exactly the audio surviving server buffer
    # clear/append/commit, plus event types (never keys or private live mic audio).
    if os.getenv("CHAT_TRACE_WIRE") and fixture_plan():
        import base64
        wire_audio = bytearray()
        wire_events = []
        original_send = service._ws_send
        async def traced_send(message):
            event_type = message.get("type")
            if event_type == "input_audio_buffer.append":
                wire_audio.extend(base64.b64decode(message["audio"]))
            else:
                wire_events.append({"at_s": session.now(), "type": event_type})
            if event_type == "input_audio_buffer.clear":
                wire_audio.clear()
            if event_type == "input_audio_buffer.commit":
                folder = HERE / ".fixtures" / "wire"
                folder.mkdir(parents=True, exist_ok=True)
                with wave.open(str(folder / f"input-{len(wire_events)}.wav"), "wb") as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                    wav.writeframes(bytes(wire_audio))
                wire_audio.clear()
            await original_send(message)
            if event_type != "input_audio_buffer.append":
                from poc_report import atomic_json
                atomic_json(HERE / ".fixtures" / "wire-events.json", wire_events)
        service._ws_send = traced_send

    # pipecat computes the per-response LLMTokenUsage at response.done but
    # never pushes the metrics frame downstream — shadow the hook on this
    # instance so the real billed usage lands in our token account
    async def _capture_llm_usage(usage: LLMTokenUsage):
        tokens.add(usage)
        raw = usage.model_dump()
        cost, basis = usage_cost(raw, cfg["model"])
        session.stage("voice_and_inference", cfg["model"],
                      session.current["endpoint_s"] if session.current else session.now(),
                      usage=raw, cost_usd=cost, pricing=basis)

    service.start_llm_usage_metrics = _capture_llm_usage

    pair = LLMContextAggregatorPair(
        LLMContext(),
        user_params=realtime_user_params(),
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
    parts += [SessionBoundary(session), service, pair.assistant(), transport.output(), transport._spk]

    # Turn reports fire from the assistant aggregator's turn-stopped event:
    # the aggregator consumes LLMFullResponseEndFrame/TTSTextFrame, so no
    # downstream frame hook can see them (turns: 0 bug in every old session).
    @pair.assistant().event_handler("on_assistant_turn_stopped")
    async def _on_assistant_turn_stopped(aggregator, msg):
        await metrics.flush_you()
        metrics.report_turn(msg.content, interrupted=msg.interrupted)
    worker = PipelineWorker(
        Pipeline(parts),
        params=PipelineParams(allow_interruptions=True),
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    transport._mic.fixture_stop = lambda: asyncio.create_task(runner.cancel())
    transport._mic.fixture_ready = lambda: service._api_session_ready
    run_task = asyncio.create_task(runner.run())

    async def mark_ready():
        while not run_task.done():
            if service._api_session_ready:
                session.ready()
                return
            await asyncio.sleep(.05)
    ready_task = asyncio.create_task(mark_ready())

    # Ctrl+C: the runner installs its own SIGINT handler when run() starts,
    # which waits for a graceful end that can hang on an active realtime
    # session. Re-install ours AFTER it starts (0.5 s later): cancel the
    # runner without hard-exiting before the session report is written.
    loop = asyncio.get_running_loop()
    import signal as _signal

    def _on_sigint():
        print(dim("— closing"), flush=True)
        asyncio.create_task(runner.cancel())
        # Let cleanup finish so the report is not lost to a forced os._exit.

    def _on_hup():
        # terminal closed — exit immediately, never linger in the background
        os._exit(0)

    async def _install_signals():
        await asyncio.sleep(0.5)  # after the runner installs its own handlers
        loop.add_signal_handler(_signal.SIGINT, _on_sigint)
        loop.add_signal_handler(_signal.SIGHUP, _on_hup)

    asyncio.create_task(_install_signals())

    print(banner("Friday (realtime) — speak, then pause. Ctrl+C to quit."))
    print(dim(f"workdir: {WORKDIR} · tools: bash, get_meeting_context"), flush=True)
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        session.error(f'{type(exc).__name__}: {exc}')
    finally:
        ready_task.cancel()
    return session


async def main():
    logger.remove()
    if os.getenv("PIPECAT_DEBUG"):
        import sys
        logger.add(sys.stderr, level="DEBUG")
        # raw server-event spy: print every realtime event type as it arrives
        import json as _json
        import pipecat.services.openai.realtime.events as _ev
        _orig = _ev.parse_server_event
        def _spy(msg):
            try:
                print(f"[evt] {_json.loads(msg).get('type')}", flush=True)
            except Exception:
                pass
            return _orig(msg)
        _ev.parse_server_event = _spy
    session = await run()
    out = session.write(HERE / "results")
    print(dim(f"session report → {out.name}"), flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")