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

    def test_every_frontend_uses_one_reasoning_model_setting(self):
        import chat
        import chat_sep
        from poc_reasoning import reasoning_model
        self.assertIs(chat.reasoning_model, reasoning_model)
        self.assertIs(chat_sep.reasoning_model, reasoning_model)
        self.assertEqual(chat_audio.THINK_MODEL, reasoning_model())
        with patch.dict('os.environ', {'CHAT_THINK_MODEL': 'test/common'}):
            self.assertEqual(chat.reasoning_model(), 'test/common')
            self.assertEqual(chat_sep.reasoning_model(), 'test/common')

    def test_audio_frontend_requires_common_reasoning_not_bash(self):
        def opener(request, **kwargs):
            payload = json.loads(request.data)
            self.assertEqual([t['function']['name'] for t in payload['tools']], ['reason'])
            self.assertEqual(payload['tool_choice']['function']['name'], 'reason')
            return self.response([])
        with patch('urllib.request.urlopen', side_effect=opener):
            asyncio.run(chat_audio.chat_stream([], None, None, delegate=True))
        from poc_reasoning import reasoning_model
        self.assertEqual(chat_audio.THINK_MODEL, reasoning_model())

    def test_realtime_cannot_bypass_reasoning_or_reanswer_original_audio(self):
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService, rt_events
        from poc_report import SessionReport
        session = SessionReport('realtime', 'test', {})
        row = session.endpoint()
        service = DelegatedRealtimeService(api_key='test', reason_session=session)
        async def exercise():
            with patch.object(service, '_ws_send', new=AsyncMock()) as send:
                await service.send_client_event(rt_events.ResponseCreateEvent())
                send.assert_not_called()
                row['reasoning_answer'] = 'Seven plus five equals twelve.'
                service.answer_row = row
                await service.send_client_event(rt_events.ResponseCreateEvent())
                payload = send.call_args.args[0]['response']
                self.assertEqual(payload['tool_choice'], 'none')
                self.assertEqual(payload['output_modalities'], ['audio'])
                self.assertEqual(payload['conversation'], 'none')
                self.assertEqual(payload['input'][0]['content'][0]['text'], row['reasoning_answer'])
                session.next_input()
                session.endpoint()
                await service.send_client_event(rt_events.ResponseCreateEvent())
                self.assertEqual(send.await_count, 1)
        asyncio.run(exercise())

    def test_realtime_uses_actual_transcript_and_ignores_stale_input(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService, OpenAIRealtimeLLMService
        from poc_report import SessionReport
        session = SessionReport('realtime', 'test', {})
        row = session.endpoint()
        reasoner = SimpleNamespace(ask=AsyncMock())
        service = DelegatedRealtimeService(api_key='test', reason_session=session, reasoner=reasoner)
        service._input_rows['input1'] = row
        async def exercise():
            with patch.object(OpenAIRealtimeLLMService, 'handle_evt_input_audio_transcription_completed', new=AsyncMock()), patch.object(service, '_create_response', new=AsyncMock()) as create:
                await service.handle_evt_input_audio_transcription_completed(
                    SimpleNamespace(item_id='input1', transcript='What is seven plus five?'))
                await asyncio.gather(*service._reason_tasks)
                self.assertEqual(reasoner.ask.call_args.args[0], 'What is seven plus five?')
                self.assertIs(service.answer_row, row)
                create.assert_awaited_once()
                session.next_input()
                service._input_rows['late'] = row
                await service.handle_evt_input_audio_transcription_completed(
                    SimpleNamespace(item_id='late', transcript='Old question'))
                self.assertEqual(reasoner.ask.await_count, 1)
        asyncio.run(exercise())

    def test_realtime_out_of_band_audio_starts_assistant_turn(self):
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService, OpenAIRealtimeLLMService, LLMFullResponseStartFrame
        from poc_report import SessionReport
        service = DelegatedRealtimeService(api_key='test', reason_session=SessionReport('realtime', 'test', {}))
        async def exercise():
            with patch.object(OpenAIRealtimeLLMService, '_handle_evt_response_created', new=AsyncMock()), patch.object(service, 'push_frame', new=AsyncMock()) as push:
                await service._handle_evt_response_created(object())
                self.assertIsInstance(push.call_args.args[0], LLMFullResponseStartFrame)
                service._current_audio_response = object()
                await service._truncate_current_audio_response()
                self.assertIsNone(service._current_audio_response)
        asyncio.run(exercise())

    def test_new_realtime_speech_cancels_bash_and_answers_followup(self):
        import tempfile
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService
        from poc_reasoning import SharedReasoner
        from poc_report import SessionReport
        async def exercise():
            with tempfile.TemporaryDirectory() as folder:
                session = SessionReport('realtime', 'test', {})
                reasoner = SharedReasoner(folder, model='test/shared', api_key='test')
                service = DelegatedRealtimeService(api_key='test', reason_session=session, reasoner=reasoner)
                def respond(messages):
                    if messages[-1]['content'] == 'Can you hear me?':
                        return 'Yes, I can hear you.', [], {'cost': .001}
                    return '', [{'id': 'long', 'name': 'bash', 'arguments': {'command': 'echo ready > ready; sleep 60'}}], {'cost': .001}
                with patch.object(reasoner, '_request', side_effect=respond), patch.object(service, '_create_response', new=AsyncMock()) as create:
                    old = session.endpoint()
                    task = asyncio.create_task(service._reason_and_speak(old, 'Run a long task.'))
                    service._reason_tasks.add(task)
                    try:
                        async def wait_ready():
                            while not (Path(folder)/'ready').exists():
                                await asyncio.sleep(.01)
                        await asyncio.wait_for(wait_ready(), 3)
                        session.next_input()
                        await asyncio.wait_for(service._handle_user_started_speaking(None), 2)
                        self.assertTrue(task.cancelled())
                        self.assertFalse(reasoner._lock.locked())
                        self.assertEqual(old['tools'][0]['status'], 'cancelled')
                        new = session.endpoint()
                        await asyncio.wait_for(service._reason_and_speak(new, 'Can you hear me?'), 2)
                        self.assertEqual(new['reasoning_answer'], 'Yes, I can hear you.')
                        self.assertIs(service.answer_row, new)
                        create.assert_awaited_once()
                    finally:
                        await service._cancel_reasoning()
        asyncio.run(exercise())

    def test_realtime_interruption_frame_cancels_background_reasoning(self):
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService, OpenAIRealtimeLLMService
        from poc_report import SessionReport
        service = DelegatedRealtimeService(api_key='test', reason_session=SessionReport('realtime', 'test', {}))
        async def exercise():
            task = asyncio.create_task(asyncio.sleep(60))
            service._reason_tasks.add(task)
            with patch.object(OpenAIRealtimeLLMService, '_handle_interruption', new=AsyncMock()) as parent:
                await service._handle_interruption()
                self.assertTrue(task.cancelled())
                parent.assert_awaited_once()
        asyncio.run(exercise())

    def test_realtime_deadline_prepares_spoken_recovery(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService
        from poc_report import SessionReport
        session = SessionReport('realtime', 'test', {})
        row = session.endpoint()
        service = DelegatedRealtimeService(api_key='test', reason_session=session,
            reasoner=SimpleNamespace(ask=AsyncMock(side_effect=asyncio.TimeoutError())))
        async def exercise():
            with patch.object(service, '_create_response', new=AsyncMock()) as create:
                await service._reason_and_speak(row, 'Long request')
                self.assertIn('too long', row['reasoning_answer'])
                self.assertTrue(row['errors'])
                create.assert_awaited_once()
        asyncio.run(exercise())

    def test_realtime_manual_commit_does_not_auto_answer(self):
        from unittest.mock import AsyncMock
        from chat import DelegatedRealtimeService, rt_events
        from poc_report import SessionReport
        session = SessionReport('realtime', 'test', {})
        service = DelegatedRealtimeService(api_key='test', reason_session=session)
        async def exercise():
            with patch.object(service, '_is_turn_detection_disabled', return_value=True), patch.object(service, 'send_client_event', new=AsyncMock()) as send:
                await service._handle_user_stopped_speaking(None)
                send.assert_awaited_once()
                self.assertIsInstance(send.call_args.args[0], rt_events.InputAudioBufferCommitEvent)
                self.assertIs(service._committed_rows[0], session.current)
        asyncio.run(exercise())

    def test_text_and_audio_callbacks_are_drained(self):
        events = [{'choices':[{'delta':{'audio':{'data':'AA==','transcript':'Hello'}}}]}]
        audio, text = [], []
        with patch('urllib.request.urlopen', return_value=self.response(events)):
            asyncio.run(chat_audio.chat_stream([], audio.append, text.append))
        self.assertEqual(audio, ['AA=='])
        self.assertEqual(text, ['Hello'])


if __name__ == '__main__':
    unittest.main()
