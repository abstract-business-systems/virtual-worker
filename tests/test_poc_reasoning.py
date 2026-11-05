"""Shared reasoning, real bash only, accounting and delegation regressions."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'experiments/pipecat-poc'))
from poc_reasoning import SharedReasoner, reasoning_model, BASH_TOOL, REASON_TOOL
from poc_report import SessionReport
from poc_tools import run_tool


class TestSharedReasoning(unittest.TestCase):
    def test_one_config_and_global_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(reasoning_model(), 'openai/gpt-4o-mini')
        with patch.dict(os.environ, {'CHAT_THINK_MODEL': 'test/shared'}):
            self.assertEqual(reasoning_model(), 'test/shared')
        with patch.dict(os.environ, {'CHAT_THINK_MODEL': ' '}):
            with self.assertRaises(ValueError): reasoning_model()
        self.assertEqual(BASH_TOOL['function']['name'], 'bash')
        self.assertEqual(REASON_TOOL['function']['name'], 'reason')

    def test_dummy_meeting_tool_removed(self):
        result = run_tool('get_meeting_context', '{}', '.')
        self.assertIn('unknown tool', result['error'])
        self.assertNotIn('next_event', result)

    def test_common_model_owns_bash_usage_and_conversation(self):
        payloads = []
        responses = [
            {'tool_calls': [{'id': 'cmd', 'function': {'name': 'bash',
                             'arguments': '{"command":"printf shared-proof"}'}}]},
            {'content': 'The result is shared-proof.'},
            {'content': 'You asked for shared-proof.'},
        ]
        def opener(request, **kwargs):
            payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': responses.pop(0)}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': .001}}).encode())
        async def exercise():
            with tempfile.TemporaryDirectory() as folder:
                reasoner = SharedReasoner(folder, model='test/shared', api_key='test')
                session = SessionReport('audio', 'test', {'think': reasoner.model})
                self.assertIn('shared-proof', await reasoner.ask('Print the proof.', session))
                row = session.current
                self.assertEqual(row['tools'][0]['result']['output'], 'shared-proof')
                self.assertEqual(len(row['stages']), 2)
                self.assertEqual(sum(s['cost_usd'] for s in row['stages']), .002)
                session.next_input()
                await reasoner.ask('What did I ask?', session)
                self.assertEqual(payloads[-1]['messages'][-2]['content'], 'The result is shared-proof.')
                self.assertIsNotNone(row['inference_end_s'])
        with patch('urllib.request.urlopen', side_effect=opener):
            asyncio.run(exercise())
        self.assertTrue(all(p['model'] == 'test/shared' for p in payloads))
        self.assertTrue(all([t['function']['name'] for t in p['tools']] == ['bash'] for p in payloads))

    def test_empty_answer_or_provider_error_does_not_enter_history(self):
        for response in ({'choices': [{'message': {'content': ''}}]}, {'error': 'unavailable'}):
            async def exercise():
                reasoner = SharedReasoner('.', model='test/shared', api_key='test')
                session = SessionReport('realtime', 'test', {})
                with self.assertRaises(RuntimeError):
                    await reasoner.ask('Question', session)
                self.assertEqual(len(reasoner.history), 1)
                self.assertIsNotNone(session.current['inference_end_s'])
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(response).encode())):
                asyncio.run(exercise())

    def test_usage_after_interruption_stays_with_original_turn(self):
        async def exercise():
            reasoner = SharedReasoner('.', model='test/shared', api_key='test')
            session = SessionReport('realtime', 'test', {})
            old = session.endpoint()
            def respond(messages):
                session.next_input()
                session.endpoint()
                return 'Stale answer', [], {'cost': .001}
            with patch.object(reasoner, '_request', side_effect=respond):
                with self.assertRaises(asyncio.CancelledError):
                    await reasoner.ask('Question', session)
            self.assertEqual(old['stages'][0]['model'], 'test/shared')
            self.assertEqual(session.current['stages'], [])
            self.assertEqual(len(reasoner.history), 1)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
