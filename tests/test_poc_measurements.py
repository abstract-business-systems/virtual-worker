"""Offline regressions for the POC; no API keys, audio hardware or models needed."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

POC = Path(__file__).resolve().parents[1]/'experiments/pipecat-poc'
sys.path.insert(0, str(POC))
from poc_report import SessionReport, first_signal, last_signal, endpoint_silence
from poc_pricing import usage_cost
from poc_tools import repair_tool_args, tool_rounds, run_tool
from token_accounting import TokenAccount, normalize_usage
from baseline import check_report
spec = importlib.util.spec_from_file_location('poc_benchmark', POC/'benchmark.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


class TestReports(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = SessionReport('separate', 'test', {'stt': 'test'}, clock=self.clock)
        self.s.ready()
        self.s.fixture_speech_end = 1.0
        self.clock.t = 1.5
        self.row = self.s.endpoint()

    def complete(self):
        self.clock.t = 2
        self.s.stage('stt', 'test', 1.5, cost_usd=.01, pricing='test')
        self.row['inference_start_s'] = 2
        self.clock.t = 4
        self.s.stage('inference', 'test', 2, cost_usd=.02, pricing='test')
        self.row['inference_end_s'] = 4
        self.clock.t = 5
        self.s.audio_received(struct.pack('<hh', 1000, 1000), 1000)
        self.s.stage('tts', 'test', 4, cost_usd=.03, pricing='test')
        self.s.playback(struct.pack('<hh', 0, 1000), 1000, delay_s=.1)
        self.s.finish_turn('An answer')
        self.clock.t = 60

    def test_round_trip_includes_endpoint_and_playback(self):
        self.complete()
        r = self.s.snapshot()['interactions'][0]
        self.assertAlmostEqual(r['round_trip_s'], 4.101)
        self.assertAlmostEqual(r['voice_latency_s'], 2.101)

    def test_voice_cost_excludes_inference_and_minute_units(self):
        self.complete()
        d = self.s.snapshot()
        self.assertAlmostEqual(d['total_cost_usd'], .06)
        self.assertAlmostEqual(d['voice_cost_per_min_usd'], .04)
        self.assertAlmostEqual(d['cost_per_min_usd'], .06)

    def test_missing_usage_is_not_free(self):
        self.s.stage('stt', 'unknown', 1.5)
        self.assertIsNone(self.s.snapshot()['total_cost_usd'])
        self.assertIsNone(self.s.snapshot()['voice_cost_usd'])

    def test_native_internal_inference_not_fabricated(self):
        self.s.mode = 'audio'
        self.row['inference_isolatable'] = False
        self.complete()
        r = self.s.snapshot()['interactions'][0]
        self.assertIsNone(r['voice_latency_s'])
        self.assertIsNone(r['voice_cost_usd'])

    def test_streaming_overlap_not_blindly_subtracted(self):
        self.complete()
        self.row['inference_end_s'] = 6
        self.assertIsNone(self.s.snapshot()['interactions'][0]['voice_latency_s'])

    def test_empty_or_inaudible_answer_is_failure(self):
        self.s.finish_turn('Text but no audio')
        self.assertEqual(self.row['status'], 'failed')
        self.assertIsNone(self.s.snapshot()['round_trip']['mean_s'])

    def test_no_dac_event_no_audible_latency(self):
        self.s.audio_received(struct.pack('<h', 1000), 1000)
        self.s.finish_turn('reply')
        self.assertIsNone(self.s.snapshot()['round_trip']['mean_s'])
        self.assertIsNotNone(self.s.snapshot()['to_audio_received']['mean_s'])

    def test_late_playback_after_api_completion_is_recorded(self):
        self.s.audio_received(struct.pack('<h', 1000), 1000)
        self.s.finish_turn('reply')
        self.clock.t = 3
        self.s.playback(struct.pack('<h', 1000), 1000)
        self.assertEqual(self.s.snapshot()['round_trip']['mean_s'], 2)

    def test_atomic_unique_reports_parse(self):
        self.complete()
        with tempfile.TemporaryDirectory() as tmp:
            p = self.s.write(Path(tmp))
            q = self.s.write(Path(tmp))
            self.assertNotEqual(p, q)
            self.assertEqual(json.loads(p.read_text())['schema_version'], 2)
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_silent_audio_is_failure_not_a_spoken_answer(self):
        self.s.audio_received(b'\0\0'*100, 1000)
        self.s.finish_turn('Text without audible speech')
        self.assertEqual(self.row['status'], 'failed')

    def test_endpoint_silence_is_validated(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'CHAT_END_SILENCE': '0.8'}):
            self.assertEqual(endpoint_silence(), .8)
        for value in ('nan', '0', '4', 'invalid'):
            with patch.dict(os.environ, {'CHAT_END_SILENCE': value}):
                with self.assertRaises(ValueError): endpoint_silence()

    def test_pcm_boundaries(self):
        data = struct.pack('<hhhh', 0, 200, -400, 0)
        self.assertEqual(first_signal(data), 1)
        self.assertEqual(last_signal(data), 2)
        self.assertIsNone(first_signal(b'\0\0'))


class TestAccounting(unittest.TestCase):
    def test_cached_audio_not_double_counted(self):
        raw = {'input_tokens': 1000, 'output_tokens': 0,
               'input_token_details': {'audio_tokens': 400, 'cached_tokens': 200,
                 'cached_tokens_details': {'audio_tokens': 100, 'text_tokens': 100}}}
        n = normalize_usage(raw)
        self.assertEqual(n['in_text'], 500)
        a = TokenAccount(); a.add(raw)
        expected = (300*10+500*.6+100*.30+100*.06)/1e6
        self.assertAlmostEqual(a.totals()['cost'], expected)
        self.assertAlmostEqual(usage_cost(raw, 'gpt-realtime-2.1-mini')[0], expected)

    def test_provider_zero_cost_not_repriced(self):
        a = TokenAccount(); a.add({'prompt_tokens': 10, 'cost': 0.0})
        self.assertEqual(a.totals()['cost'], 0)
        self.assertEqual(usage_cost({'prompt_tokens': 10, 'cost': 0.0}, 'unknown')[0], 0)

    def test_repricing_is_a_sidecar_and_missing_usage_stays_unknown(self):
        raw = {'schema_version': 2, 'measurement_duration_s': 60, 'total_cost_usd': 9,
               'interactions': [{'stages': [{'model': 'openai/gpt-4o-mini',
                   'usage': {'prompt_tokens': 100, 'cost': .01}, 'cost_usd': 9}]}]}
        self.assertEqual(benchmark.repriced_estimates(raw)['repriced_total_cost_usd'], .01)
        self.assertEqual(raw['total_cost_usd'], 9)
        raw['interactions'][0]['stages'].append({'model': 'gpt-4o-mini-transcribe', 'usage': None})
        result = benchmark.repriced_estimates(raw)
        self.assertIsNone(result['repriced_total_cost_usd'])
        self.assertEqual(result['repriced_known_cost_usd'], .01)

    def test_unknown_model_not_priced_as_mini(self):
        self.assertIsNone(usage_cost({'prompt_tokens': 100}, 'unknown')[0])


class TestTools(unittest.TestCase):
    def test_concatenated_arguments_and_whitespace(self):
        self.assertEqual(repair_tool_args('{} {"command":"pwd"}  '), {'command': 'pwd'})

    def test_malformed_arguments_do_not_execute_partial_command(self):
        for value in ('{"command":"rm ', '["pwd"]', 'junk {"command":"pwd"}'):
            with self.assertRaises(ValueError): repair_tool_args(value)

    def test_workdir_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'note.txt').write_text('safe fixture')
            self.assertEqual(run_tool('bash', {'command': 'cat note.txt'}, tmp)['output'], 'safe fixture')
            self.assertIn('error', run_tool('other', {}, tmp))

    def test_all_parallel_calls_are_answered_then_followup_runs(self):
        requests, executions = [], []
        async def request(msgs):
            requests.append(list(msgs))
            return ('', [{'id': 'a', 'name': 'bash', 'arguments': '{}{"command":"pwd"}'},
                         {'id': 'b', 'name': 'bash', 'arguments': '{"command":"ls"}'}]) if len(requests)==1 else ('Done', [])
        async def execute(name, args):
            executions.append((name, args)); return {'output': 'data'}
        result = asyncio.run(tool_rounds([], request, execute))
        self.assertEqual(result, 'Done')
        self.assertEqual(len(executions), 2)
        self.assertEqual([m['tool_call_id'] for m in requests[1] if m['role']=='tool'], ['a','b'])
        self.assertEqual(json.loads(requests[1][0]['tool_calls'][0]['function']['arguments']), {'command':'pwd'})

    def test_unbounded_and_empty_model_responses_fail(self):
        async def execute(*args): return {}
        async def endless(msgs): return '', [{'name':'bash', 'arguments':'{}'}]
        async def empty(msgs): return '', []
        with self.assertRaises(RuntimeError): asyncio.run(tool_rounds([], endless, execute, max_rounds=2))
        with self.assertRaises(RuntimeError): asyncio.run(tool_rounds([], empty, execute))


class TestCollator(unittest.TestCase):
    def report(self, data, name='audio-test.json'):
        folder = POC/'results'
        # Must be under ROOT for provenance; remove temporary source afterward.
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            path = Path(tmp)/name
            path.write_text(json.dumps(data))
            return benchmark.read_row(path)

    def test_seconds_to_minutes_and_missing_model(self):
        row, _ = self.report({'turns':2, 'wall_total_s':120, 'tokens':{'totals':{'cost':.6}}})
        self.assertAlmostEqual(row['cost_per_min_usd'], .3)
        self.assertEqual(row['model'], 'unrecorded')

    def test_missing_duration_and_zero_cost(self):
        row, _ = self.report({'turns':1, 'tokens':{'totals':{'cost':0.0}}})
        self.assertEqual(row['cost_usd'], 0)
        self.assertIsNone(row['cost_per_min_usd'])

    def test_conflicting_cost_is_flagged(self):
        row, _ = self.report({'turns':1, 'total_cost_usd':0, 'tokens':{'totals':{'cost':.02}}})
        self.assertEqual(row['cost_usd'], .02)
        self.assertIn('conflicting', row['warnings'])

    def test_modern_failed_sessions_retained(self):
        row, _ = self.report({'schema_version':2, 'mode':'audio', 'engine':'test', 'turns':0,
                             'models':{'voice':'full'}, 'total_cost_usd':None}, 'session-test.json')
        self.assertIsNone(row['cost_usd'])
        self.assertIn('full', row['model'])

    def test_invalid_json_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'audio-test.json'; p.write_text('{} }')
            row, reason = benchmark.read_row(p)
            self.assertIsNone(row); self.assertIn('invalid_report', reason)


class TestBaselineGate(unittest.TestCase):
    def sample(self):
        return {'fixture_results': [{'completed': True, 'heuristic_keyword_match': True}],
                'completed_turns': 1, 'round_trip': {'samples': 1}, 'errors': []}

    def test_good_operational_sample(self):
        self.assertTrue(check_report(self.sample()))
        self.assertFalse(check_report({}))

    def test_incorrect_answer_does_not_pass_as_completed_baseline(self):
        data = self.sample()
        data['fixture_results'][0]['heuristic_keyword_match'] = False
        self.assertFalse(check_report(data))

    def test_expected_interruption_requires_verified_tool_cancellation(self):
        data = self.sample()
        data['fixture_results'].insert(0, {'completed': False, 'expected_interruption': True})
        self.assertFalse(check_report(data))
        data['fixture_results'][0]['interruption_verified'] = True
        self.assertTrue(check_report(data))

    def test_missing_playback_sample_does_not_pass(self):
        data = self.sample()
        data['round_trip']['samples'] = 0
        self.assertFalse(check_report(data))


class TestLauncher(unittest.TestCase):
    def test_optional_pipecat_variable_unset_and_mode_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            uv = Path(tmp)/'uv'
            uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            uv.chmod(0o755)
            env = dict(os.environ, PATH=tmp+':'+os.environ['PATH'])
            env.pop('CHAT_SEP_PIPECAT', None)
            for args, expected in [(['separate'],'chat_audio.py'),
                                   (['separate','--pipecat'],'chat_sep.py'),
                                   (['audio'],'chat_audio.py'), (['realtime'],'chat.py')]:
                result = subprocess.run([str(POC/'run.sh')]+args, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)


if __name__ == '__main__':
    unittest.main()
