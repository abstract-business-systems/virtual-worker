"""Real-process regressions: cancellation frees the conversation and kills bash.

No paid API calls or audio devices. HTTP model responses are controlled, but
bash and its child processes are real and cancellation runs through real tasks.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'experiments/pipecat-poc'))
from poc_tools import run_tool_async
from poc_reasoning import SharedReasoner
from poc_report import SessionReport

COMMAND = 'echo $$ > parent.pid; sleep 60 & echo $! > child.pid; wait'


async def wait_for_file(path):
    async def poll():
        while not path.exists() or not path.read_text().strip():
            await asyncio.sleep(.01)
    await asyncio.wait_for(poll(), 3)


def process_running(pid):
    try:
        # An orphaned child may briefly be a zombie awaiting init's reap.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class TestCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_bash_and_child(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            task = asyncio.create_task(run_tool_async('bash', {'command': COMMAND}, folder))
            try:
                await wait_for_file(path/'child.pid')
                pids = [int((path/name).read_text()) for name in ('parent.pid', 'child.pid')]
                self.assertTrue(all(process_running(pid) for pid in pids))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertFalse(any(process_running(pid) for pid in pids))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_output_callback_failure_also_stops_process(self):
        with tempfile.TemporaryDirectory() as folder:
            def broken_callback(text):
                raise RuntimeError('observer failed')
            with self.assertRaises(RuntimeError):
                await run_tool_async('bash', {'command': 'echo $$ > pid; echo ready; sleep 60'},
                                     folder, on_output=broken_callback)
            self.assertFalse(process_running(int((Path(folder)/'pid').read_text())))

    async def test_tool_timeout_also_stops_children(self):
        with tempfile.TemporaryDirectory() as folder:
            result = await run_tool_async('bash', {'command': COMMAND}, folder, timeout=.1)
            self.assertTrue(result['timed_out'])
            for name in ('parent.pid', 'child.pid'):
                self.assertFalse(process_running(int((Path(folder)/name).read_text())))

    async def test_cancel_during_process_spawn_does_not_orphan_child(self):
        real_spawn = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        child = None
        async def delayed_spawn(*args, **kwargs):
            nonlocal child
            child = await real_spawn(*args, **kwargs)
            spawned.set()
            await release.wait()
            return child
        with patch('asyncio.create_subprocess_exec', side_effect=delayed_spawn):
            task = asyncio.create_task(run_tool_async('bash', {'command': 'sleep 60'}, '.'))
            await asyncio.wait_for(spawned.wait(), 3)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertIsNotNone(child.returncode)

    async def test_interrupted_bash_releases_reasoning_lock_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as folder:
            session = SessionReport('realtime', 'test', {})
            reasoner = SharedReasoner(folder, model='test/shared', api_key='test')
            def respond(messages):
                if messages[-1]['content'] == 'Can you hear me?':
                    return 'Yes, I can hear you.', [], {'cost': .001}
                return '', [{'id': 'long', 'name': 'bash', 'arguments': {'command': COMMAND}}], {'cost': .001}
            with patch.object(reasoner, '_request', side_effect=respond):
                task = asyncio.create_task(reasoner.ask('Run a long task.', session))
                try:
                    await wait_for_file(Path(folder)/'child.pid')
                    old = session.current
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                    self.assertEqual(old['tools'][0]['status'], 'cancelled')
                    self.assertFalse(reasoner._lock.locked())
                    session.next_input()
                    answer = await asyncio.wait_for(reasoner.ask('Can you hear me?', session), 2)
                    self.assertEqual(answer, 'Yes, I can hear you.')
                    self.assertNotIn('Run a long task.', str(reasoner.history))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_overall_deadline_stops_tool_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            session = SessionReport('audio', 'test', {})
            reasoner = SharedReasoner(folder, model='test/shared', api_key='test', turn_timeout=.2)
            response = ('', [{'id': 'long', 'name': 'bash', 'arguments': {'command': COMMAND}}], {'cost': .001})
            with patch.object(reasoner, '_request', return_value=response):
                with self.assertRaises(asyncio.TimeoutError):
                    await reasoner.ask('Long task', session)
            self.assertFalse(reasoner._lock.locked())
            self.assertEqual(session.current['tools'][0]['status'], 'cancelled')
            self.assertFalse(process_running(int((Path(folder)/'child.pid').read_text())))

    async def test_cancelled_http_waiter_does_not_block_followup(self):
        import threading
        started, release = threading.Event(), threading.Event()
        reasoner = SharedReasoner('.', model='test/shared', api_key='test')
        session = SessionReport('audio', 'test', {})
        def respond(messages):
            if messages[-1]['content'] == 'Blocked HTTP':
                started.set()
                release.wait(3)
            return 'Ready', [], {'cost': .001}
        with patch.object(reasoner, '_request', side_effect=respond):
            task = asyncio.create_task(reasoner.ask('Blocked HTTP', session))
            try:
                while not started.is_set():
                    await asyncio.sleep(.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                session.next_input()
                self.assertEqual(await asyncio.wait_for(reasoner.ask('Next', session), 2), 'Ready')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
