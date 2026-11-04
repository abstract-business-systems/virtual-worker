"""Replay a bounded, multi-turn fixture plan through the real CLI pipeline.

The same feeder is used by realtime, separate/Pipecat and the HTTP engine.
No microphone is opened in fixture mode. A completed fixture is not proof of
human conversational quality; transcript keyword checks are explicitly heuristic.
"""
import asyncio
import json
import os
import time
import wave
from pathlib import Path


def fixture_plan():
    if os.getenv('CHAT_TEST_PLAN'):
        path = Path(os.environ['CHAT_TEST_PLAN']).resolve()
        data = json.loads(path.read_text())
        for case in data['cases']:
            case['wav'] = str((path.parent / case['wav']).resolve())
        return data
    if os.getenv('CHAT_TEST_WAV'):
        return {'cases': [{'id': 'single-wav', 'wav': os.environ['CHAT_TEST_WAV']}],
                'minimum_duration_s': 0}
    return None


async def feed_plan(plan, session, enqueue, rate, is_playing, stop, *, ready=lambda: True):
    from pipecat.audio.utils import create_stream_resampler
    session.turn_done = asyncio.Event()
    for _ in range(300):
        if ready():
            break
        await asyncio.sleep(.1)
    else:
        session.error('Fixture setup timed out before connection was ready')
        stop()
        return
    session.ready()
    test_start = time.perf_counter()
    try:
        import itertools
        cases = plan['cases']
        if not cases:
            raise ValueError('Fixture plan has no cases')
        for index, case in enumerate(itertools.cycle(cases)):
            if index >= len(cases) and time.perf_counter()-test_start >= plan.get('minimum_duration_s', 0):
                break
            session.next_input()
            session.fixture_label = case['id']
            session.turn_done.clear()
            with wave.open(case['wav'], 'rb') as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError('Fixtures must be PCM16 mono WAV')
                pcm = wav.readframes(wav.getnframes())
                source_rate = wav.getframerate()
            converter = create_stream_resampler()
            if rate != source_rate:
                pcm = await converter.resample(pcm, source_rate, rate)
            from poc_report import last_signal
            last = last_signal(pcm)
            if last is None:
                raise ValueError(f"Silent fixture: {case['id']}")
            # The feed clock, not upload completion, defines end of speech.
            start = session.now()
            session.fixture_speech_end = start + (last+1)/rate
            size = int(rate/10)*2
            for offset in range(0, len(pcm), size):
                if offset/2 <= last < (offset+size)/2:
                    session.fixture_speech_end = session.now()+(last-offset/2+1)/rate
                enqueue(pcm[offset:offset+size])
                await asyncio.sleep(len(pcm[offset:offset+size])/2/rate)
            # Real 100 ms silence blocks exercise VAD and endpointing normally.
            for _ in range(15):
                enqueue(b'\0\0'*int(rate/10))
                await asyncio.sleep(.1)
            try:
                await asyncio.wait_for(session.turn_done.wait(), timeout=60)
            except asyncio.TimeoutError:
                session.endpoint()
                session.error(f"No completed response within 60s: {case['id']}")
                session.finish_turn()
            # Wait for DAC playback, not merely the API response to finish.
            deadline = time.perf_counter()+45
            while is_playing() and time.perf_counter() < deadline:
                await asyncio.sleep(.1)
            if is_playing():
                raise RuntimeError('Playback did not drain within 45s')
            await asyncio.sleep(.5)
            row = session.current or {}
            reply = row.get('reply') or ''
            words = case.get('expected_any', [])
            session.fixture_results.append({
                'id': case['id'], 'completed': row.get('status') == 'completed',
                'heuristic_keyword_match': (any(w.lower() in reply.lower() for w in words)
                                            if words else None),
                'human_quality_review': 'pending',
            })
            # Stop early on a broken baseline instead of repeatedly billing failures.
            if row.get('status') != 'completed':
                session.error('Fixture run stopped after a failed interaction')
                break
    except Exception as exc:
        session.error(f'Fixture failure: {type(exc).__name__}: {exc}')
    finally:
        stop()
