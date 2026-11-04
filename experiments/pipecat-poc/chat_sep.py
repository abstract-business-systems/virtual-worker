#!/usr/bin/env python3
"""Optional Pipecat-native separate pipeline, sharing the v2 measurement schema.

./run.sh separate --pipecat [--plan plan.json]
Streaming LLM and TTS can overlap. We retain their timestamps and leave the
inference-excluded latency null when a simple serial subtraction is invalid.
"""
import asyncio
import configparser
import json
import os
import signal
from pathlib import Path

from loguru import logger
logger.remove()

from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import WorkerRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService

import token_accounting as ta
from chat import MicInput, SpeakerOutput, TurnMetrics, HERE
from cli_ui import banner, dim, tool, you
from poc_pipeline import SessionBoundary
from poc_pricing import usage_cost, tts_cost
from poc_report import SessionReport, load_environment
from poc_tools import run_tool

load_environment(HERE)
WORKDIR = os.environ.get('CHAT_WORKDIR', os.getcwd())


async def run():
    presets = configparser.ConfigParser()
    presets.read(HERE/'models.ini')
    think = os.getenv('CHAT_THINK_MODEL') or presets.get('separate', 'think')
    voice = os.getenv('CHAT_TTS_VOICE') or presets.get('separate', 'voice').split(':')[-1]
    stt_model = os.getenv('CHAT_STT_MODEL', 'gpt-4o-mini-transcribe')
    if os.getenv('CHAT_MODEL'):
        override = os.environ['CHAT_MODEL']
        if not override.startswith('openai-tts:'):
            raise SystemExit('Native separate --model accepts openai-tts:<voice>; use CHAT_THINK_MODEL for inference')
        voice = override.split(':', 1)[1]
    if not os.getenv('OPENAI_API_KEY') or not os.getenv('OPENROUTER_API_KEY'):
        raise SystemExit('OPENAI_API_KEY and OPENROUTER_API_KEY are required')
    session = SessionReport('separate', 'pipecat',
        {'stt': stt_model, 'think': think, 'tts': 'gpt-4o-mini-tts', 'voice': voice})
    tokens = ta.TokenAccount()
    metrics = TurnMetrics(tokens, session)

    class MeasuredSTT(OpenAISTTService):
        async def _transcribe(self, audio):
            started = session.now()
            try:
                result = await super()._transcribe(audio)
            except Exception:
                session.stage('stt', stt_model, started)
                raise
            usage = getattr(result, 'usage', None)
            raw = usage.model_dump() if hasattr(usage, 'model_dump') else usage
            cost, basis = usage_cost(raw, stt_model)
            session.stage('stt', stt_model, started, usage=raw, cost_usd=cost, pricing=basis)
            session.endpoint()['input_text'] = result.text
            print(you(f'You: {result.text}'), flush=True)
            return result

    class MeasuredLLM(OpenAILLMService):
        async def _process_context(self, context):
            row = session.endpoint()
            if row['inference_start_s'] is None:
                row['inference_start_s'] = session.now()
            self._request_start = session.now()
            try:
                return await super()._process_context(context)
            finally:
                row['inference_end_s'] = session.now()

        def _token_usage(self, usage):
            raw = usage.model_dump()
            cost, basis = usage_cost(raw, think)
            session.stage('inference', think, self._request_start,
                          usage=raw, cost_usd=cost, pricing=basis)
            return super()._token_usage(usage)

    class MeasuredTTS(OpenAITTSService):
        async def run_tts(self, text, context_id):
            started, seconds = session.now(), 0.0
            try:
                async for frame in super().run_tts(text, context_id):
                    if isinstance(frame, TTSAudioRawFrame):
                        seconds += len(frame.audio)/2/frame.sample_rate
                    yield frame
            finally:
                cost, basis = tts_cost(seconds) if seconds else (None, 'no_tts_audio')
                session.stage('tts', 'gpt-4o-mini-tts', started, cost_usd=cost, pricing=basis)

    mic, speaker = MicInput(session), SpeakerOutput(session)
    mic.fixture_playing = lambda: speaker.playing
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(
        params=VADParams(min_volume=float(os.getenv('CHAT_MIN_VOLUME', '.15')),
                         start_secs=.2, stop_secs=.3)))
    stt = MeasuredSTT(api_key=os.environ['OPENAI_API_KEY'], model=stt_model)
    llm = MeasuredLLM(api_key=os.environ['OPENROUTER_API_KEY'],
        base_url='https://openrouter.ai/api/v1', model=think)
    tts = MeasuredTTS(api_key=os.environ['OPENAI_API_KEY'], model='gpt-4o-mini-tts', voice=voice)

    async def execute(params):
        row = session.endpoint()
        if len(row['tools']) >= 8:
            session.error('Tool loop exceeded 8 calls')
            await params.result_callback({'error': 'Tool limit reached; stop and explain the failure'})
            return
        name = params.function_name
        result = await asyncio.to_thread(run_tool, name, params.arguments, WORKDIR)
        row['tools'].append({'name': name, 'arguments': params.arguments, 'result': result})
        print(tool(f'[tool] {name} -> {json.dumps(result)[:160]}'), flush=True)
        await params.result_callback(result)

    schemas = [
        FunctionSchema(name='get_meeting_context', description='Get stub meeting/calendar context.',
                       properties={}, required=[]),
        FunctionSchema(name='bash', description='Run bash in the working directory to inspect documents.',
                       properties={'command': {'type': 'string'}}, required=['command']),
    ]
    for schema in schemas:
        llm.register_function(schema.name, execute)
    context = LLMContext(tools=ToolsSchema(standard_tools=schemas))
    context.add_message({'role': 'system', 'content':
        'You are Friday. Keep spoken answers brief. Your working directory is '+WORKDIR+
        '. Use bash to inspect files when asked about documents. Use get_meeting_context '
        'for calendar questions; it is demonstration data, not a real calendar.'})
    pair = LLMContextAggregatorPair(context)

    @pair.assistant().event_handler('on_assistant_turn_stopped')
    async def report_turn(aggregator, message):
        metrics.report_turn(message.content, interrupted=message.interrupted)

    # Assistant aggregator belongs at the END. Flow-control frames must be
    # forwarded upstream by metrics; otherwise tool follow-up TTS stalls.
    worker = PipelineWorker(Pipeline([
        mic, vad, SessionBoundary(session, vad_only=True), stt, pair.user(),
        llm, tts, metrics, speaker, pair.assistant(),
    ]), params=PipelineParams(allow_interruptions=True, enable_metrics=True, enable_usage_metrics=True))
    runner = WorkerRunner()
    await runner.add_workers(worker)
    mic.fixture_stop = lambda: asyncio.create_task(runner.cancel())
    session.ready()
    task = asyncio.create_task(runner.run())
    loop = asyncio.get_running_loop()

    async def signals():
        await asyncio.sleep(.5)
        loop.add_signal_handler(signal.SIGINT, mic.fixture_stop)
        loop.add_signal_handler(signal.SIGTERM, mic.fixture_stop)
    signal_task = asyncio.create_task(signals())
    print(banner('Friday (separate · pipecat) — speak, then pause. Ctrl+C to quit.'))
    print(dim(f'workdir: {WORKDIR} · stt {stt_model} · think {think} · voice {voice}'), flush=True)
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        session.error(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        signal_task.cancel()
        out = session.write(HERE/'results')
        print(dim(f'session report → {out.name}'), flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
