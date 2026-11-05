"""Read-only Friday bridge and real backend context ACL, without private data."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in ('common', 'backend', 'experiments/pipecat-poc'):
    sys.path.insert(0, str(ROOT/folder))
from poc_worker import WorkerBridge
from vw_backend.kb import KnowledgeBase
from vw_backend.server import Backend


class TestWorkerBridge(unittest.IsolatedAsyncioTestCase):
    async def test_real_context_acl_does_not_leak_other_active_meeting(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = Backend.__new__(Backend)
            backend.kb = KnowledgeBase(str(Path(folder)/'kb.db'))
            backend.kb.add_membership('product-operator', 'Product')
            product = backend.kb.ensure_team('Product')
            backend.kb.kb_put('Product launch is Wednesday', product)
            meeting_id, _ = backend.kb.start_meeting('meet', 'synthetic-url', 'Sales')
            backend.kb.append_transcript(meeting_id, 1, 'synthetic-participant', 'Sales private zebra budget')
            seen = []
            async def serve(reader, writer):
                try:
                    msg = json.loads(await reader.readline())
                    seen.append(msg)
                    result = backend.dispatch(msg)
                    writer.write((json.dumps({'req_id': msg['req_id'], 'ok': True, 'data': result})+'\n').encode())
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            path = str(Path(folder)/'worker.sock')
            server = await asyncio.start_unix_server(serve, path)
            try:
                context = await WorkerBridge(path, 'product-operator').context('Product launch')
                self.assertTrue(context['allowed'])
                self.assertEqual(context['transcript'], [])
                self.assertNotIn('zebra', json.dumps(context))
                self.assertIn('Wednesday', json.dumps(context))
                denied = await WorkerBridge(path, 'unknown-user').context('Sales budget')
                self.assertFalse(denied['allowed'])
                self.assertEqual(denied['transcript'], [])
                self.assertTrue(all(m['type'] == 'context.get' and m['in_meeting'] is False for m in seen))
                # An explicitly selected foreign meeting is also filtered.
                selected = backend.h_context_get({'requester': 'product-operator',
                    'question': 'budget', 'meeting_id': meeting_id, 'in_meeting': False})
                self.assertEqual(selected['transcript'], [])
                from types import SimpleNamespace
                captured = []
                def compose(question, recent, *args):
                    captured.append(recent)
                    return 'synthetic answer'
                backend.brain = SimpleNamespace(compose_answer=compose,
                    act=lambda text, **kwargs: kwargs['context'])
                backend.agent = None
                backend.h_question({'requester': 'product-operator', 'text': 'budget'})
                self.assertEqual(captured, [[]])
                self.assertNotIn('zebra', backend.h_brain_act({
                    'requester': 'product-operator', 'text': 'budget'}))
            finally:
                server.close()
                await server.wait_closed()

    async def test_unavailable_worker_is_a_tool_error_not_a_hang(self):
        with tempfile.TemporaryDirectory() as folder:
            result = await WorkerBridge(str(Path(folder)/'missing'), 'operator').context('question')
            self.assertIn('error', result)

    async def test_opt_in_requires_identity_and_socket(self):
        with patch.dict('os.environ', {'CHAT_WORKER_SOCKET': '', 'CHAT_WORKER_REQUESTER': ''}):
            self.assertIsNone(WorkerBridge.from_environment())
        with patch.dict('os.environ', {'CHAT_WORKER_SOCKET': '/synthetic/socket', 'CHAT_WORKER_REQUESTER': ''}):
            with self.assertRaises(ValueError):
                WorkerBridge.from_environment()

    async def test_timeout_closes_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            closed = asyncio.Event()
            async def serve(reader, writer):
                try:
                    await reader.read()
                    closed.set()
                finally:
                    writer.close()
                    await writer.wait_closed()
            path = str(Path(folder)/'worker.sock')
            server = await asyncio.start_unix_server(serve, path)
            try:
                result = await WorkerBridge(path, 'operator', timeout=.05).context('question')
                self.assertIn('error', result)
                await asyncio.wait_for(closed.wait(), 1)
            finally:
                server.close()
                await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
