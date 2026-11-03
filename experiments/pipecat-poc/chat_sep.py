#!/usr/bin/env python3
"""separate mode — rebuilt on the pipecat framework (M9 quality work).

The framework owns mic capture, Silero VAD, turn detection, and
interruptions; the voice stages are pipecat services (no hand-rolled
audio plumbing):

  mic → VAD → OpenAISTTService (gpt-4o-mini-transcribe, online)
      → user aggregator → OpenAILLMService (OpenRouter think model,
        native tool calls) → OpenAITTSService (gpt-4o-mini-tts voice)
      → turn metrics → speaker

Run: ./run.sh separate --pipecat   (self-test: CHAT_TEST_WAV=<wav>)
Ctrl+C to quit.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
if not os.environ.get("CHAT_PIPECAT_LOGS"):
    logger.remove()  # kill pipecat's import-time banner (set CHAT_PIPECAT_LOGS=1 to see service errors)

from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService

import token_accounting as ta
from chat import MicInput, SpeakerOutput, Spy, TurnMetrics, load_env
from cli_ui import banner, dim, err, friday, stage, warn, you
from chat_audio import run_tool, WORKDIR

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
if not OPENROUTER_KEY:
    sys.exit("OPENROUTER_API_KEY not set (experiments/pipecat-poc/.env)")

THINK_MODEL = os.environ.get("CHAT_MODEL") or "openai/gpt-4o-mini"
TTS_VOICE = os.environ.get("CHAT_TTS_VOICE", "coral")
STT_MODEL = os.environ.get("CHAT_STT_MODEL", "gpt-4o-mini-transcribe")
# TTS estimate: audio/speech reports no usage; documented density ~15 tok/s,
# gpt-4o-mini-tts audio out ~$12/M
TTS_TOK_PER_SEC = 15
TTS_USD_PER_TOK = 12e-6

SYSTEM_PROMPT = (
    "You are Friday, a virtual worker. Keep spoken answers brief and "
    "conversational — they will be spoken aloud. Use the bash tool to "
    "list, read, or summarize files in the working directory when asked "
    "about documents or files, and get_meeting_context for meeting or "
    "calendar questions."
)

TOOL_SCHEMAS = [
    FunctionSchema(
        name="get_meeting_context",
        description="Get the current meeting/calendar context for Friday.",
        properties={},
        required=[],
    ),
    FunctionSchema(
        name="bash",
        description="Run a bash command in the working directory and "
                    "return its stdout/stderr. Use it to list, read, or "
                    "summarize files in the working directory.",
        properties={
            "command": {"type": "string",
                        "description": "The bash command to run"},
        },
        required=["command"],
    ),
]


async def get_meeting_context(params: FunctionCallParams):
    await params.result_callback(run_tool("get_meeting_context", "{}"))


async def bash(params: FunctionCallParams):
    await params.result_callback(
        run_tool("bash", json.dumps(params.arguments or {})))


class SttEcho(FrameProcessor):
    """Pass-through between STT and the user aggregator: prints the real
    transcription as 'You:' — the aggregator downstream consumes
    TranscriptionFrames, so metrics never sees them."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            print(you(f"You: {frame.text.strip()}"), flush=True)
        await self.push_frame(frame, direction)


class SepMetrics(TurnMetrics):
    """TurnMetrics + real-STT 'You:' display (no local whisper needed:
    the STT service already returns exactly what the think model hears)."""

    def __init__(self, tokens: ta.TokenAccount):
        super().__init__(tokens)
        self.t_stt_done: float | None = None
        self._trace: dict = {}

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if direction != FrameDirection.DOWNSTREAM:
            # TurnMetrics drops upstream frames (the realtime engine never
            # needs them) — but the non-realtime TTS flow-control frames DO
            # travel upstream; without this pass-through the second LLM
            # response (after a tool call) never streams and TTS stays
            # silent
            await self.push_frame(frame, direction)
            return
        await super().process_frame(frame, direction)
        if os.environ.get("CHAT_FRAME_TRACE"):
            n = type(frame).__name__
            if n not in ("InputAudioRawFrame",):
                self._trace[n] = self._trace.get(n, 0) + 1

    def _show_you(self):
        pass  # framework STT already displayed it; skip local whisper



    def report_turn(self, text: str, interrupted: bool = False):
        """TurnMetrics.report_turn + TTS estimate (audio/speech reports
        no usage)."""
        if not interrupted and self.audio_secs_out:
            secs = self.audio_secs_out
            out_tok = round(secs * TTS_TOK_PER_SEC)
            self.tokens.add({
                "prompt_tokens": 0, "completion_tokens": out_tok,
                "input_audio_tokens": 0, "output_audio_tokens": out_tok,
                "cost": round(out_tok * TTS_USD_PER_TOK, 6)})
        super().report_turn(text, interrupted=interrupted)
        if self.t_stt_done is not None and self.t_turn_end is not None:
            pass  # stage times are implied by first-audio latency


async def run() -> dict:
    tokens = _ACCOUNT
    metrics = SepMetrics(tokens)

    mic = MicInput()
    spk = SpeakerOutput()
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(min_volume=0.15, start_secs=0.2, stop_secs=0.3)))

    stt = OpenAISTTService(api_key=OPENAI_KEY, model=STT_MODEL)
    llm = OpenAILLMService(
        api_key=OPENROUTER_KEY,
        base_url="https://openrouter.ai/api/v1",
        model=THINK_MODEL,
    )
    tts = OpenAITTSService(api_key=OPENAI_KEY, voice=TTS_VOICE,
                           model="gpt-4o-mini-tts")
    llm.register_function("get_meeting_context", get_meeting_context)
    llm.register_function("bash", bash)

    ctx = LLMContext(tools=ToolsSchema(standard_tools=TOOL_SCHEMAS))
    ctx.add_message({"role": "system", "content": SYSTEM_PROMPT})
    pair = LLMContextAggregatorPair(ctx)

    @pair.assistant().event_handler("on_assistant_turn_stopped")
    async def _on_assistant_turn_stopped(aggregator, msg):
        await metrics.flush_you()
        metrics.report_turn(msg.content, interrupted=msg.interrupted)

    debug = bool(os.getenv("CHAT_DEBUG"))
    # canonical pipecat order — the assistant aggregator sits at the END
    # (it observes frames to build context); between llm and tts it
    # swallows TTSTextFrames and usage MetricsFrames (TTS stayed silent)
    tail_metrics = metrics
    if os.environ.get("CHAT_NO_METRICS"):
        class _Passthrough(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                n = type(frame).__name__
                self._trace = getattr(self, "_trace", {})
                self._trace[n] = self._trace.get(n, 0) + 1
                await self.push_frame(frame, direction)
        tail_metrics = _Passthrough()
    parts = [mic, vad, stt, SttEcho(), pair.user(), llm, tts, tail_metrics,
             spk, pair.assistant()]
    if debug:
        parts.insert(len(parts) - 1, Spy("pre-output"))

    worker = PipelineWorker(
        Pipeline(parts),
        params=PipelineParams(allow_interruptions=True),
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())

    loop = asyncio.get_running_loop()
    import signal as _signal

    def _on_sigint():
        print(dim("— closing"), flush=True)
        asyncio.create_task(runner.cancel())
        loop.call_later(3.0, lambda: os._exit(0))

    def _on_hup():
        os._exit(0)

    async def _install_signals():
        await asyncio.sleep(0.5)  # after the runner installs its own
        loop.add_signal_handler(_signal.SIGINT, _on_sigint)
        loop.add_signal_handler(_signal.SIGHUP, _on_hup)

    asyncio.create_task(_install_signals())

    print(banner(f"Friday (separate · pipecat) — speak, then pause. "
                 f"Ctrl+C to quit."))
    print(dim(f"workdir: {WORKDIR} · stt {STT_MODEL} · think {THINK_MODEL} · "
              f"voice gpt-4o-mini-tts:{TTS_VOICE} · tools: bash, "
              f"get_meeting_context"), flush=True)
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    return {
        "turns": metrics.turns,
        "total_cost_usd": round(metrics.total_cost_usd, 4),
        "tts_audio_secs": round(metrics.audio_secs_out, 2),
        "frame_trace": getattr(tail_metrics, "_trace", {}),
        "wall_total_s": round(time.perf_counter() - metrics._t0, 1),
    }


async def main():
    result = await run()
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"sep-pipecat-{stamp}.json"
    out.write_text(json.dumps({**result, "engine": "pipecat",
                               "tokens": tokens_report()},
                              indent=1, default=str))
    if result.get("frame_trace"):
        print(dim(f"frames seen by metrics: {result['frame_trace']}"),
              flush=True)
    print(dim(f"session: {result.get('turns', 0)} turns · tts audio "
              f"{result.get('tts_audio_secs', 0):.1f}s · "
              f"{result.get('wall_total_s', 0)}s → {out.name}"), flush=True)


def tokens_report():
    return _ACCOUNT.report()


_ACCOUNT = ta.TokenAccount()  # module-level: run() feeds it, main() reports it

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")