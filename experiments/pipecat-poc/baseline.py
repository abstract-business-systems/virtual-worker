#!/usr/bin/env python3
"""Replay the same six tasks through existing CLIs; no alternate voice engine.

Default: approximately five minutes of INTERACTIONS per approach (not padded
idle time). --smoke runs the six tasks once. Requires model keys for paid runs;
--prepare-only synthesizes input fixtures locally with espeak-ng, at no API cost.
Human listening/quality review and bill/pricing verification are always pending.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone

from poc_report import atomic_json

HERE = Path(__file__).resolve().parent
CASES = [
    ('answer', 'What is seven plus five? Answer in one short sentence.', ['twelve', '12']),
    ('memory-set', 'Remember this for our conversation. The project code name is Copper Finch. Say the code name.', ['copper', 'finch']),
    ('memory-recall', 'What project code name did I just give you?', ['copper', 'finch']),
    ('files', 'Use bash to list the files in the current working directory. Tell me the file names.', ['benchmark-notes']),
    ('summary', 'Use bash to read benchmark-notes dot txt in the current directory and summarize its contents.', ['wednesday', 'onboarding', 'ravi']),
    ('context', 'According to benchmark-notes dot txt, which room-related action is still open? Use bash if needed.', ['training room', 'confirm the room']),
]


def prepare(folder: Path, duration: float):
    synth = shutil.which('espeak-ng') or shutil.which('espeak')
    if not synth:
        raise SystemExit('Install espeak-ng or supply your own fixture plan; no packages were installed.')
    folder.mkdir(parents=True, exist_ok=True)
    workdir = folder/'documents'
    workdir.mkdir(exist_ok=True)
    (workdir/'benchmark-notes.txt').write_text(
        'The onboarding review is on Wednesday. Ravi will prepare the checklist.\n'
        'The remaining action is to confirm the training room.\n')
    cases = []
    for name, question, expected in CASES:
        wav = folder/(name+'.wav')
        subprocess.run([synth, '-s', '155', '-w', str(wav), question], check=True)
        cases.append({'id': name, 'wav': wav.name, 'question': question, 'expected_any': expected})
    plan = folder/'plan.json'
    atomic_json(plan, {'source': 'local espeak-ng; synthetic speech, not human-quality evidence',
                       'minimum_duration_s': duration, 'cases': cases})
    return plan, workdir


def check_report(data):
    """Operational/keyword gate only, never a substitute for human quality review."""
    fixtures = data.get('fixture_results') or []
    return (bool(fixtures) and not data.get('errors')
            and all((r.get('interruption_verified') is True if r.get('expected_interruption') else
                     r.get('completed') and r.get('heuristic_keyword_match') is not False
                     and r.get('background_state_verified') is not False)
                    for r in fixtures)
            and data.get('round_trip', {}).get('samples') == data.get('completed_turns'))


def run_mode(mode, plan, workdir, native, timeout, *, model=None, think_model=None):
    existing = set((HERE/'results').glob('session-*.json'))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    logs = HERE/'results/logs'
    logs.mkdir(parents=True, exist_ok=True)
    logfile = logs/f'{mode}-{stamp}.log'
    command = [str(HERE/'run.sh'), mode, '--plan', str(plan), '--folder', str(workdir)]
    if native and mode == 'separate':
        command.append('--pipecat')
    if model:
        command.extend(['--model', model])
    env = dict(os.environ, CHAT_NO_TRANSCRIPT='1', PYTHONUNBUFFERED='1')
    if think_model:
        env['CHAT_THINK_MODEL'] = think_model
    with logfile.open('w') as stream:
        child = subprocess.Popen(command, cwd=HERE, env=env, stdout=stream,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            returncode = child.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
    reports = sorted(set((HERE/'results').glob('session-*.json'))-existing)
    checks_passed = bool(reports) and all(check_report(json.loads(p.read_text())) for p in reports)
    return {'mode': mode, 'exit_code': returncode, 'fixture_checks_passed': checks_passed,
            'reports': [p.name for p in reports], 'log': str(logfile.relative_to(HERE))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['all', 'realtime', 'audio', 'separate'], default='all')
    parser.add_argument('--smoke', action='store_true', help='one six-task pass, not the M10 five-minute sample')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--pause-regression', action='store_true', help='three realtime repetitions of the naturally paused arithmetic request')
    parser.add_argument('--native-separate', action='store_true')
    parser.add_argument('--interrupt-regression', action='store_true',
                        help='interrupt a real sleeping bash task, then ask a question')
    parser.add_argument('--background-regression', action='store_true',
                        help='start a background job, converse, inspect it, explicitly cancel it')
    args = parser.parse_args()
    if sum((args.pause_regression, args.interrupt_regression, args.background_regression)) > 1:
        parser.error('Choose only one regression suite')
    short_run = args.smoke or args.pause_regression or args.interrupt_regression or args.background_regression
    plan, workdir = prepare(HERE/'.fixtures', 0 if short_run else 300)
    if args.pause_regression:
        if args.mode not in ('all', 'realtime'):
            parser.error('--pause-regression is a realtime test')
        data = json.loads(plan.read_text())
        data['cases'] = [dict(data['cases'][0], id=f'pause-regression-{n}') for n in (1, 2, 3)]
        atomic_json(plan, data)
    if args.interrupt_regression:
        question = ('Use bash to run sleep thirty, then echo finished. '
                    'This is a finite test command. Run it before answering.')
        synth = shutil.which('espeak-ng') or shutil.which('espeak')
        wav = plan.parent/'interrupt-long-task.wav'
        subprocess.run([synth, '-s', '155', '-w', str(wav), question], check=True)
        data = json.loads(plan.read_text())
        data['allow_interruptions'] = True
        data['cases'] = [
            {'id': 'interrupt-long-task', 'wav': wav.name, 'question': question,
             'interrupt_on_tool': True}, data['cases'][0]]
        atomic_json(plan, data)
    if args.background_regression:
        data = json.loads(plan.read_text())
        data['allow_interruptions'] = True
        specs = [
            ('background-start', 'Start a background bash job that prints the numbers one through one hundred twenty, with a one second pause between each number. Return its job ID immediately.', 'background_start', 'running'),
            ('background-conversation', CASES[0][1], None, 'running'),
            ('background-status', 'Check the background job status and tell me its latest output.', 'job_status', 'running'),
            ('background-cancel', 'Cancel the background job now and confirm it stopped.', 'job_cancel', 'cancelled')]
        synth = shutil.which('espeak-ng') or shutil.which('espeak')
        data['cases'] = []
        for name, question, expected_tool, expected_status in specs:
            wav = plan.parent/(name+'.wav')
            subprocess.run([synth, '-s', '155', '-w', str(wav), question], check=True)
            data['cases'].append({'id': name, 'wav': wav.name, 'question': question,
                'expected_tool': expected_tool, 'expected_job_status': expected_status,
                'expect_numeric_progress': name in ('background-start', 'background-status'),
                'expected_any': ['12', 'twelve'] if name == 'background-conversation' else []})
        atomic_json(plan, data)
    print(f'Fixture plan: {plan}', flush=True)
    if args.prepare_only:
        return
    modes = (['realtime'] if args.pause_regression else
             ['realtime', 'audio', 'separate'] if args.mode == 'all' else [args.mode])
    results = []
    for mode in modes:
        print(f'Running {mode}; model API requests incur cost. Logs retained locally.', flush=True)
        result = run_mode(mode, plan, workdir, args.native_separate, 240 if short_run else 600)
        results.append(result)
        print(json.dumps(result), flush=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    atomic_json(HERE/'results'/f'baseline-{stamp}.json', {
        'kind': 'baseline_index', 'smoke': short_run, 'pause_regression': args.pause_regression,
        'interrupt_regression': args.interrupt_regression, 'background_regression': args.background_regression,
        'human_quality_review': 'pending', 'runs': results})
    if any(r['exit_code'] != 0 or not r['fixture_checks_passed'] for r in results):
        raise SystemExit('At least one run failed; inspect its log and reports before comparing numbers.')


if __name__ == '__main__':
    main()
