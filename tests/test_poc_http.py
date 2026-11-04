"""HTTP adapter regressions; run with the POC interpreter for audio dependencies."""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'experiments/pipecat-poc'))
HAS_AUDIO = importlib.util.find_spec('sounddevice') is not None
if HAS_AUDIO:
    import chat_audio


@unittest.skipUnless(HAS_AUDIO, 'Run with experiments/pipecat-poc/.venv/bin/python')
class TestHTTP(unittest.TestCase):
    def response(self, events):
        data = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
        return io.BytesIO(data.encode())

    def test_realtime_pins_stop_policy_without_transcript_wait(self):
        from poc_pipeline import realtime_user_params
        params = realtime_user_params()
        self.assertEqual(params.vad_analyzer.params.stop_secs, .8)
        strategy = params.user_turn_strategies.stop[0]
        self.assertFalse(strategy.wait_for_transcript)
        self.assertEqual(strategy._user_speech_timeout, 0.0)
        asyncio.run(params.vad_analyzer.cleanup())

    def test_stream_tool_ids_and_indices_and_provider_cost(self):
        events = [
            {'choices':[{'delta':{'tool_calls':[{'index':1,'id':'second','function':{'name':'bash','arguments':'{"command":'}}]}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'first','function':{'name':'bash','arguments':'{}'}}]}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':1,'function':{'arguments':'"pwd"}'}}]}}]},
            {'choices':[], 'usage':{'prompt_tokens':20,'completion_tokens':10,'cost':.001}},
        ]
        with patch('urllib.request.urlopen', return_value=self.response(events)):
            calls, usage = asyncio.run(chat_audio.chat_stream([], None, None))
        self.assertEqual([c['id'] for c in calls], ['first','second'])
        self.assertEqual(json.loads(calls[1]['arguments']), {'command':'pwd'})
        self.assertEqual(usage['cost'], .001)

    def test_stream_errors_are_not_silently_successful(self):
        with patch('urllib.request.urlopen', return_value=self.response([{'error':{'message':'broken'}}])):
            with self.assertRaises(RuntimeError): asyncio.run(chat_audio.chat_stream([], None, None))

    def test_http_errors_are_not_silently_successful(self):
        error = urllib.error.HTTPError('https://example.test', 400, 'Bad request', {}, io.BytesIO(b'unsupported format'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, '400'): asyncio.run(chat_audio.chat_stream([], None, None))

    def test_tools_disabled_for_audio_followup(self):
        def opener(request, **kwargs):
            payload = json.loads(request.data)
            self.assertNotIn('tools', payload)
            self.assertTrue(payload['stream_options']['include_usage'])
            return self.response([])
        with patch('urllib.request.urlopen', side_effect=opener):
            asyncio.run(chat_audio.chat_stream([], None, None, allow_tools=False))

    def test_text_and_audio_callbacks_are_drained(self):
        events = [{'choices':[{'delta':{'audio':{'data':'AA==','transcript':'Hello'}}}]}]
        audio, text = [], []
        with patch('urllib.request.urlopen', return_value=self.response(events)):
            asyncio.run(chat_audio.chat_stream([], audio.append, text.append))
        self.assertEqual(audio, ['AA=='])
        self.assertEqual(text, ['Hello'])


if __name__ == '__main__':
    unittest.main()
