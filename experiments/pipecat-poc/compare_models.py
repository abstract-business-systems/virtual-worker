#!/usr/bin/env python3
"""Bounded automatic voice-model sweep; shared thinking model stays fixed.

Paid, sequential runs: concurrent speakers would contaminate playback timings.
Synthetic operational checks are not human-quality or bill verification.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal

from baseline import HERE, prepare, run_mode
from poc_reasoning import reasoning_model
from poc_report import atomic_json, load_environment

CANDIDATES = {
    'realtime': ('realtime', 'gpt-realtime-2.1-mini'),
    'audio-full': ('audio', 'openai/gpt-audio'),
    'audio-mini': ('audio', 'openai/gpt-audio-mini'),
    'separate': ('separate', 'openai-tts:coral'),
}


def summarize(data, think):
    rows = data.get('interactions', [])
    stages = [s for r in rows for s in r.get('stages', []) if s['component'] == 'inference']
    observed = sorted({s['model'] for s in stages})
    def normalized(text):
        return re.sub(r'[^\w]', '', (text or '').lower())
    pairs = [r for r in rows if r.get('reasoning_answer') and r.get('reply')]
    return {
        'think_models_observed': observed,
        'shared_thinking_verified': bool(stages) and observed == [think],
        'measurement_duration_s': data.get('measurement_duration_s'),
        'completed_turns': data.get('completed_turns'), 'turns': data.get('turns'),
        'round_trip': data.get('round_trip'), 'voice_latency': data.get('voice_latency'),
        'total_cost_usd': data.get('total_cost_usd'),
        'known_cost_usd': data.get('known_cost_usd'),
        'cost_per_min_usd': data.get('cost_per_min_usd'),
        'voice_cost_per_min_usd': data.get('voice_cost_per_min_usd'),
        'provider_transcript_verbatim_fraction': (
            sum(normalized(r['reasoning_answer']) == normalized(r['reply']) for r in pairs)/len(pairs)
            if pairs else None),
        'verbatim_scope': 'provider transcript vs reasoning text, punctuation/case ignored; not acoustic or semantic quality',
        'errors': data.get('errors', []),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', action='append', choices=list(CANDIDATES))
    parser.add_argument('--duration', type=float, default=300)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not 0 < args.duration <= 600:
        parser.error('duration must be greater than zero and at most 600 seconds')
    load_environment(HERE)
    think = reasoning_model()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    plan, workdir = prepare(HERE/'.fixtures'/('comparison-'+stamp), 0 if args.smoke else args.duration)
    output = HERE/'results'/('comparison-'+stamp+'.json')
    index = {'kind': 'model_comparison_index', 'thinking_model': think,
        'synthetic_input': True, 'human_quality_review': 'pending', 'pricing_review': 'pending',
        'smoke': args.smoke, 'minimum_duration_s': 0 if args.smoke else args.duration,
        'plan': str(plan.relative_to(HERE)), 'runs': []}
    if args.prepare_only:
        print(plan)
        return
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        for name in args.candidate or list(CANDIDATES):
            mode, voice = CANDIDATES[name]
            print(f'Running {name}; paid API calls. Shared thinking: {think}', flush=True)
            try:
                run = run_mode(mode, plan, workdir, False, 240 if args.smoke else args.duration+180,
                               model=voice, think_model=think)
                run.update(candidate=name, voice_model=voice)
                run['summaries'] = [summarize(json.loads((HERE/'results'/p).read_text()), think)
                                    for p in run['reports']]
                run['comparison_eligible'] = (run['exit_code'] == 0 and run['fixture_checks_passed']
                    and bool(run['summaries']) and all(s['shared_thinking_verified'] for s in run['summaries']))
            except Exception as exc:
                # Retain the failed candidate, do not substitute another model or retry endlessly.
                run = {'candidate': name, 'voice_model': voice, 'comparison_eligible': False,
                       'failure': f'{type(exc).__name__}: {exc}'}
            index['runs'].append(run)
            atomic_json(output, index)
            print(json.dumps(run), flush=True)
    except KeyboardInterrupt:
        index['stopped_early'] = True
        atomic_json(output, index)
        raise SystemExit('Comparison stopped; partial results retained')
    print(f'Comparison index: {output}', flush=True)
    if not all(r['comparison_eligible'] for r in index['runs']):
        raise SystemExit('Some candidates failed; retained for review, not approved baselines')


if __name__ == '__main__':
    main()
