"""Versioned, honest measurements shared by all three POC engines.

All timestamps use perf_counter. Playback is a PortAudio DAC estimate, not
an acoustic/Teams measurement. Native speech models' internal inference is
NOT observable: their voice-only cost/latency must remain null.
"""
from __future__ import annotations

import json
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path


def endpoint_silence() -> float:
    """A short clause pause must not commit a turn and cancel its continuation."""
    import math
    value = float(os.getenv('CHAT_END_SILENCE', '0.8'))
    if not math.isfinite(value) or not 0.1 <= value <= 3.0:
        raise ValueError('CHAT_END_SILENCE must be between 0.1 and 3.0 seconds')
    return value


def first_signal(pcm: bytes, threshold: int = 150) -> int | None:
    """First non-silent PCM16 mono sample (energy proxy, not a word detector)."""
    return next((i for i, (v,) in enumerate(struct.iter_unpack('<h', pcm[:len(pcm)//2*2]))
                 if abs(v) >= threshold), None)


def last_signal(pcm: bytes, threshold: int = 150) -> int | None:
    samples = memoryview(pcm[:len(pcm)//2*2]).cast('h')
    return next((i for i in range(len(samples)-1, -1, -1)
                 if abs(samples[i]) >= threshold), None)


def load_environment(here: Path) -> None:
    """Local secrets are optional; explicit environment always wins."""
    import shlex
    path = here / '.env'
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        parts = shlex.split(raw, comments=True)
        if parts and '=' in parts[0]:
            key, value = parts[0].removeprefix('export ').split('=', 1)
            os.environ.setdefault(key, value)


def atomic_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


class SessionReport:
    def __init__(self, mode: str, engine: str, models: dict, *, clock=time.perf_counter):
        self.clock = clock
        self.t0 = clock()
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.mode, self.engine, self.models = mode, engine, models
        self.settings = {}
        self.records: list[dict] = []
        self.current: dict | None = None
        self.last_speech_at: float | None = None
        self.input_audio_s = 0.0
        self.errors: list[str] = []
        self.ready_at: float | None = None
        self.finished_at: float | None = None
        self.fixture_label: str | None = None
        self.fixture_speech_end: float | None = None
        self.fixture_results: list[dict] = []
        self.turn_done = None  # optional asyncio.Event, installed by fixture feeder

    def now(self):
        return self.clock() - self.t0

    def ready(self):
        if self.ready_at is None:
            self.ready_at = self.now()

    def observe_input(self, pcm: bytes, rate: int, *, at: float | None = None):
        self.input_audio_s += len(pcm) / 2 / rate
        sample = last_signal(pcm)
        if sample is not None:
            # Capture blocks arrive after their samples. The last active sample
            # removes trailing silence; live values remain an energy estimate.
            end = self.now() if at is None else at
            self.last_speech_at = end - (len(pcm)/2 - sample - 1)/rate

    def endpoint(self):
        if self.current is not None:
            return self.current
        speech_end = self.fixture_speech_end
        source = 'fixture_sample_boundary' if speech_end is not None else 'input_energy_proxy'
        if speech_end is None:
            speech_end = self.last_speech_at
        self.current = {
            'id': len(self.records) + 1, 'fixture': self.fixture_label,
            'speech_end_s': speech_end, 'speech_end_source': source,
            'endpoint_s': self.now(), 'first_audio_received_s': None,
            'first_playback_s': None, 'playback_measurement': None,
            'output_audio_s': 0.0, 'input_text': None, 'reply': None,
            'status': 'running', 'errors': [], 'stages': [], 'tools': [],
            'inference_start_s': None, 'inference_end_s': None,
            'inference_isolatable': self.mode == 'separate',
        }
        self.records.append(self.current)
        return self.current

    def audio_received(self, pcm: bytes, rate: int):
        row = self.endpoint()
        if row['first_audio_received_s'] is None and first_signal(pcm) is not None:
            row['first_audio_received_s'] = self.now()
        row['output_audio_s'] += len(pcm)/2/rate

    def playback(self, pcm: bytes, rate: int, *, delay_s: float = 0.0):
        row = self.current
        sample = first_signal(pcm)
        if row is not None and row['first_playback_s'] is None and sample is not None:
            row['first_playback_s'] = self.now() + max(0.0, delay_s) + sample/rate
            row['playback_measurement'] = 'portaudio_dac_estimate_not_acoustic'

    def stage(self, component: str, model: str, started: float, *, usage=None,
              cost_usd: float | None = None, pricing: str = 'unavailable', row=None):
        row = self.endpoint() if row is None else row
        row['stages'].append({
            'component': component, 'model': model, 'start_s': started,
            'end_s': self.now(), 'usage': usage, 'cost_usd': cost_usd,
            'pricing': pricing,
        })

    def error(self, message: str):
        self.errors.append(message)
        if self.current is not None:
            self.current['errors'].append(message)

    def finish_turn(self, reply: str = '', *, interrupted: bool = False):
        row = self.current
        if row is None:
            return
        row['reply'] = reply.strip()
        row['finished_s'] = self.now()
        row['status'] = ('interrupted' if interrupted else 'failed' if row['errors'] or
                         not row['reply'] or row['first_audio_received_s'] is None else 'completed')
        # Do not clear current yet: audio may still be queued for playback.
        if self.turn_done is not None:
            self.turn_done.set()

    def next_input(self):
        if self.current is not None and self.current['status'] == 'running':
            self.finish_turn(interrupted=True)
        self.current = None
        self.fixture_speech_end = None
        self.fixture_label = None

    def snapshot(self):
        now = self.finished_at if self.finished_at is not None else self.now()
        rows = []
        for original in self.records:
            r = dict(original)
            if r['status'] == 'running':
                r['status'] = 'incomplete'
            end = r['speech_end_s']
            heard = r['first_playback_s']
            received = r['first_audio_received_s']
            r['round_trip_s'] = max(0, heard-end) if heard is not None and end is not None else None
            r['to_audio_received_s'] = max(0, received-end) if received is not None and end is not None else None
            a, b = r['inference_start_s'], r['inference_end_s']
            duration = b-a if a is not None and b is not None else None
            r['inference_with_tools_s'] = duration
            # Subtract only a measured, complete inference window before playback.
            r['voice_latency_s'] = (max(0, r['round_trip_s']-duration)
                if r['inference_isolatable'] and r['round_trip_s'] is not None
                and duration is not None and b <= heard else None)
            stages = r['stages']
            costs = [s['cost_usd'] for s in stages]
            r['cost_complete'] = bool(costs) and all(c is not None for c in costs)
            r['known_cost_usd'] = sum(c for c in costs if c is not None)
            r['cost_usd'] = r['known_cost_usd'] if r['cost_complete'] else None
            voice = [s for s in stages if s['component'] in ('stt', 'tts')]
            r['voice_cost_usd'] = (sum(s['cost_usd'] for s in voice)
                if r['inference_isolatable'] and voice
                and {'stt', 'tts'} <= {s['component'] for s in voice}
                and all(s['cost_usd'] is not None for s in voice) else None)
            rows.append(r)
        costs = [r['cost_usd'] for r in rows]
        voice = [r['voice_cost_usd'] for r in rows]
        total = sum(costs) if costs and all(c is not None for c in costs) else None
        voice_total = sum(voice) if voice and all(c is not None for c in voice) else None
        wall = max(0, now - self.ready_at) if self.ready_at is not None else None
        successful = [r for r in rows if r['status'] == 'completed']
        def avg(key):
            values = [r[key] for r in successful if r[key] is not None]
            return {'mean_s': sum(values)/len(values) if values else None, 'samples': len(values)}
        return {
            'schema_version': 2, 'mode': self.mode, 'engine': self.engine,
            'models': self.models, 'settings': self.settings, 'started_utc': self.started_utc,
            'wall_total_s': now, 'measurement_duration_s': wall,
            'cost_denominator': 'established-session wall minutes, including listening, inference and playback',
            'turns': len(rows), 'completed_turns': len(successful),
            'input_audio_s': self.input_audio_s, 'total_cost_usd': total,
            'known_cost_usd': sum(r['known_cost_usd'] for r in rows),
            'voice_cost_usd': voice_total,
            'cost_per_min_usd': total*60/wall if total is not None and wall else None,
            'voice_cost_per_min_usd': voice_total*60/wall if voice_total is not None and wall else None,
            'round_trip': avg('round_trip_s'), 'voice_latency': avg('voice_latency_s'),
            'to_audio_received': avg('to_audio_received_s'),
            'quality_verified': False, 'pricing_verified': False,
            'inference_isolation': ('explicit text inference window (includes tool time)'
                if self.mode == 'separate' else
                'external task inference recorded separately; native voice processing remains bundled'
                if self.settings.get('task_reasoning') == 'shared_external_model' else
                'native model inference is bundled; not independently measurable'),
            'errors': self.errors, 'fixture_results': self.fixture_results,
            'interactions': rows,
        }

    def write(self, folder: Path):
        self.finished_at = self.now()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path = folder / f'session-{self.mode}-{stamp}.json'
        atomic_json(path, self.snapshot())
        return path
