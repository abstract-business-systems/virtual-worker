#!/usr/bin/env python3
"""M7 POC — the same realtime flow wrapped in a minimal Pipecat pipeline.

Compares against the raw websocket probe (experiments/realtime-model-poc/):
first-audio latency, audio streaming behavior, tool calling mid-voice,
actual usage/cost. Headless: passthrough source/sink processors, no
Daily/LiveKit, no mic. Question audio reused from the raw probe.

Run: uv run --project experiments/pipecat-poc experiments/pipecat-poc/probe.py
"""

import asyncio
import json
import os
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallFromLLM, FunctionCallParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.services.openai.realtime import events as rt_events
from pipecat.workers.runner import WorkerRunner

HERE = Path(__file__).parent
QUESTION = HERE.parent / "realtime-model-poc" / "fixtures" / "question.wav"
PUSH_CHUNK_MS = 200


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
        "model": cfg.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini"),
    }


def load_question() -> bytes:
    with wave.open(str(QUESTION), "rb") as w:
        return w.readframes(w.getnframes())


class ProbeSink(FrameProcessor):
    """Pipeline exit: records output timing, text, and tool calls."""

    def __init__(self):
        super().__init__()
        self.t0: float | None = None
        self.first_audio: float | None = None
        self.audio_chunks: list[float] = []
        self.audio_bytes: int = 0
        self.sample_rate = 24000
        self.text: list[str] = []
        self.tool_calls: list[str] = []
        self.response_end: float | None = None
        self.errors: list[str] = []
        self._seen: set = set()

    def now(self) -> float:
        return round(time.perf_counter() - self.t0, 3) if self.t0 is not None else None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, StartFrame):
            if self._setup:
                self.sample_rate = self._setup.audio_out_sample_rate
            self.t0 = time.perf_counter()
        elif self.t0 is None:
            return
        elif isinstance(frame, TTSAudioRawFrame):
            self.audio_bytes += len(frame.audio)
            t = self.now()
            if self.first_audio is None:
                self.first_audio = t
            self.audio_chunks.append(t)
        elif isinstance(frame, TTSTextFrame):
            self.text.append(frame.text)
        elif isinstance(frame, FunctionCallFromLLM):
            self.tool_calls.append(frame.function_name)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.response_end = self.now()
        if os.getenv("TRACE_FRAMES") and type(frame).__name__ not in self._seen:
            self._seen.add(type(frame).__name__)
            print("SINK-TYPE:", type(frame).__name__, flush=True)
        elif isinstance(frame, ErrorFrame):
            self.errors.append(str(frame))


class ProbeSource(FrameProcessor):
    """Pipeline entry: forwards every frame downstream (pipecat 1.15 does
    not auto-forward — processors must push frames they want passed on)."""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class TracedProbeService(OpenAIRealtimeLLMService):
    """Prints client events (except audio appends) when TRACE_FRAMES is set."""

    async def _ws_send(self, msg):
        t = msg.get("type", "?")
        if t == "session.update":
            print("PROBE-SEND:", t, msg["session"].get("audio", {}).get("input"), flush=True)
        elif t != "input_audio_buffer.append":
            print("PROBE-SEND:", t, flush=True)
        return await super()._ws_send(msg)


async def run(cfg: dict) -> dict:
    sink = ProbeSink()
    source = ProbeSource()

    tool_called = asyncio.Event()

    async def get_meeting_context(params: FunctionCallParams):
        tool_called.set()
        await params.result(
            json.dumps(
                {"recent_meeting": "M7 POC scoping", "next_event": "Friday demo review"}
            )
        )

    service_cls = TracedProbeService if os.getenv("TRACE_FRAMES") else OpenAIRealtimeLLMService
    service = service_cls(
        api_key=cfg["api_key"],
        settings=OpenAIRealtimeLLMService.Settings(
            model=cfg["model"],
            system_instruction=(
                "You are Friday, a virtual worker. Acknowledge briefly, then "
                "call get_meeting_context before answering."
            ),
            session_properties=rt_events.SessionProperties(
                audio=rt_events.AudioConfiguration(
                    input=rt_events.AudioInput(
                        format=rt_events.PCMAudioFormat(rate=24000),
                        # OpenAI's server-side VAD detects speech but never
                        # auto-commits on gpt-realtime-2.1-mini (verified raw,
                        # 4 configurations) — turns are client-driven; the
                        # service commits + response.create on
                        # UserStoppedSpeakingFrame.
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

    worker = PipelineWorker(
        Pipeline([source, service, sink]),
        params=PipelineParams(allow_interruptions=True),
        idle_timeout_secs=None,
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    # Wait for StartFrame to reach the sink (pipeline fully up).
    for _ in range(200):
        if sink.t0 is not None:
            break
        await asyncio.sleep(0.05)
    if sink.t0 is None:
        raise RuntimeError("Pipeline did not start within 10 s")

    audio = load_question()
    chunk = int(24000 * 2 * PUSH_CHUNK_MS / 1000)
    t_push_start = time.perf_counter()
    sink.t0 = t_push_start  # measure from first audio push (like the raw probe)
    for i in range(0, len(audio), chunk):
        await worker.queue_frame(
            InputAudioRawFrame(audio=audio[i:i + chunk], sample_rate=24000, num_channels=1)
        )
        await asyncio.sleep(PUSH_CHUNK_MS / 1000)
    t_last_input = time.perf_counter() - t_push_start
    await asyncio.sleep(0.3)
    await worker.queue_frame(UserStartedSpeakingFrame())
    await worker.queue_frame(UserStoppedSpeakingFrame())
    t_commit = time.perf_counter() - t_push_start

    # Wait for the tool call and the audio response to finish (or timeout).
    deadline = time.perf_counter() + 45
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.1)
        if tool_called.is_set() and sink.audio_chunks:
            await asyncio.sleep(2.0)  # let trailing chunks and transcript arrive
            break

    await worker.stop_when_done()
    await run_task

    return {
        "t_last_input_chunk_s": round(t_last_input, 3),
        "t_turn_commit_s": round(t_commit, 3),
        "t_first_audio_chunk_s": sink.first_audio,
        "t_first_audio_from_commit_s": round(sink.first_audio - t_commit, 3) if sink.first_audio else None,
        "t_response_end_s": sink.response_end,
        "audio_chunk_count": len(sink.audio_chunks),
        "audio_span_s": round(sink.audio_chunks[-1] - sink.audio_chunks[0], 3) if sink.audio_chunks else None,
        "audio_bytes_raw": sink.audio_bytes,
        "audio_seconds": round(sink.audio_bytes / 2 / sink.sample_rate, 2) if sink.sample_rate else None,
        "tool_call": {"name": "get_meeting_context", "called": tool_called.is_set()},
        "transcript": "".join(sink.text).strip(),
        "errors": sink.errors,
    }


async def main():
    logger.remove()
    if os.getenv("PIPECAT_DEBUG"):
        import sys
        logger.add(sys.stderr, level="DEBUG")
    cfg = load_env()
    t_start = time.perf_counter()
    result = await run(cfg)
    result["wall_total_s"] = round(time.perf_counter() - t_start, 3)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"run-{stamp}.json"
    out_file.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"results -> {out_file}")


if __name__ == "__main__":
    asyncio.run(main())