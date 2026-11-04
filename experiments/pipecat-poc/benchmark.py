#!/usr/bin/env python3
"""Collate reports without inventing models, durations, missing usage or quality.

Raw historical reports stay untouched. Invalid JSON is surfaced in rejected.json.
This is a dataset collator, NOT the model-swap runner (baseline.py replays tests).
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
from poc_report import atomic_json

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT/'docs/benchmarks'
FAMILIES = {'chat-': ('realtime', 'pipecat'), 'audio-': ('audio', 'chat_audio'),
            'separate-': ('separate', 'chat_audio'), 'sep-pipecat-': ('separate', 'pipecat')}


def read_row(path: Path):
    try:
        d = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f'invalid_report: {exc}'
    if not isinstance(d, dict):
        return None, 'report_is_not_an_object'
    if d.get('kind') == 'baseline_index':
        return None, 'baseline_index_not_a_session'
    if path.name.startswith('run-'):
        return None, 'legacy_probe_not_a_session'
    family = next((v for prefix, v in FAMILIES.items() if path.name.startswith(prefix)), None)
    modern = d.get('schema_version') == 2
    if not modern and not family:
        return None, 'unknown_report_family'
    if not modern and not d.get('turns'):
        return None, 'legacy_zero_turn_debug_session'
    warnings = []
    tokens = (d.get('tokens') or {}).get('totals') or {}
    cost = d.get('total_cost_usd')
    token_cost = tokens.get('cost')
    if not modern and token_cost is not None:
        if cost is not None and abs(cost-token_cost) > 1e-6:
            warnings.append('conflicting_cost_totals; using_token_total')
        cost = token_cost
    duration = d.get('measurement_duration_s') if modern else d.get('wall_total_s')
    if not modern:
        warnings.extend(['legacy_models_or_pricing_not_verified', 'legacy_latency_not_M10_comparable'])
    if duration is None:
        warnings.append('duration_missing; no_per_minute_number')
    model_data = d.get('models') or d.get('model')
    if not model_data:
        warnings.append('model_not_recorded; do_not_infer_from_filename')
    mode, engine = (d.get('mode'), d.get('engine')) if modern else family
    if modern and d.get('errors'):
        warnings.append('session_has_errors')
    if modern and any(r.get('heuristic_keyword_match') is False for r in d.get('fixture_results', [])):
        warnings.append('fixture_keyword_check_failed; not_a_validated_baseline')
    if modern and not d.get('pricing_verified'):
        warnings.append('pricing_not_bill_verified')
    rel = str(path.resolve().relative_to(ROOT))
    return {
        'ts': d.get('started_utc') or path.stem.rsplit('-', 1)[-1],
        'schema_version': d.get('schema_version', 1), 'mode': mode,
        'engine': d.get('engine') or engine,
        'model': json.dumps(model_data, sort_keys=True) if model_data else 'unrecorded',
        'turns': d.get('turns', 0), 'completed_turns': d.get('completed_turns'),
        'wall_s': duration, 'tok_in': tokens.get('in'),
        'tok_in_audio': tokens.get('in_audio'), 'tok_cached': tokens.get('cached'),
        'tok_out': tokens.get('out'), 'tok_out_audio': tokens.get('out_audio'),
        'cost_usd': cost,
        'cost_per_turn_usd': cost/d['turns'] if cost is not None and d.get('turns') else None,
        'cost_per_min_usd': cost*60/duration if cost is not None and duration and duration > 0 else None,
        'voice_cost_per_min_usd': d.get('voice_cost_per_min_usd') if modern else None,
        'round_trip_mean_s': (d.get('round_trip') or {}).get('mean_s') if modern else None,
        'round_trip_samples': (d.get('round_trip') or {}).get('samples') if modern else None,
        'voice_latency_mean_s': (d.get('voice_latency') or {}).get('mean_s') if modern else None,
        'quality_verified': d.get('quality_verified', False),
        'pricing_verified': d.get('pricing_verified', False),
        'warnings': '; '.join(warnings), 'src': rel,
    }, None


def row_from(path):
    """Compatibility for callers; main() also retains rejection reasons."""
    return read_row(Path(path))[0]


def main():
    rows, rejected = [], []
    for path in sorted((ROOT/'experiments').glob('*/results/*.json')):
        row, reason = read_row(path)
        if row is not None:
            rows.append(row)
        else:
            rejected.append({'src': str(path.relative_to(ROOT)), 'reason': reason})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json(OUT_DIR/'sessions.json', rows)
    atomic_json(OUT_DIR/'rejected.json', rejected)
    columns = list(rows[0]) if rows else ['src']
    with (OUT_DIR/'sessions.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    print(f'{len(rows)} sessions, {len(rejected)} excluded/invalid reports → docs/benchmarks/')


if __name__ == '__main__':
    main()
