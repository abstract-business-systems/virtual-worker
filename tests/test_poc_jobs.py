"""Managed jobs: real processes, no model/API/audio dependencies."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'experiments/pipecat-poc'))
from poc_jobs import JobManager
from poc_reasoning import SharedReasoner
from poc_report import SessionReport


async def wait_output(jobs, job_id, word):
    async def poll():
        while word not in jobs.status(job_id)['output']:
            await asyncio.sleep(.01)
    await asyncio.wait_for(poll(), 3)


class TestJobs(unittest.IsolatedAsyncioTestCase):
    async def test_live_output_one_job_and_explicit_cancel(self):
        notifications = []
        jobs = JobManager('.', notify=notifications.append)
        try:
            job = await jobs.execute('background_start', {'command': 'printf ready; sleep 60'})
            await wait_output(jobs, job['job_id'], 'ready')
            self.assertEqual(jobs.status(job['job_id'])['status'], 'running')
            self.assertIn('error', jobs.start('echo should-not-run'))
            result = await jobs.cancel(job['job_id'])
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(notifications[0]['status'], 'cancelled')
            self.assertEqual(len(jobs.status()['jobs']), 1)
        finally:
            await jobs.close()

    async def test_completion_failure_and_timeout(self):
        jobs = JobManager('.')
        try:
            for command, timeout, expected in [('printf done', 2, 'completed'),
                    ('exit 7', 2, 'failed'), ('sleep 60', .05, 'timed_out')]:
                job = jobs.start(command, timeout)
                await asyncio.gather(*list(jobs.tasks.values()))
                self.assertEqual(jobs.status(job['job_id'])['status'], expected)
        finally:
            await jobs.close()

    async def test_close_before_start_and_reject_new_work(self):
        jobs = JobManager('.')
        job = jobs.start('sleep 60')
        await jobs.close()
        self.assertEqual(jobs.status(job['job_id'])['status'], 'cancelled')
        self.assertIn('error', jobs.start('echo no'))

    async def test_output_and_history_are_bounded(self):
        jobs = JobManager('.')
        try:
            job = jobs.start("python3 -c 'print(\"x\" * 1000000)'", 3)
            await asyncio.gather(*list(jobs.tasks.values()))
            self.assertLessEqual(len(jobs.status(job['job_id'])['output']), 4000)
            for _ in range(22):
                jobs.start('true')
                await asyncio.gather(*list(jobs.tasks.values()))
            self.assertEqual(len(jobs.jobs), 20)
        finally:
            await jobs.close()

    async def test_validation(self):
        jobs = JobManager('.')
        for timeout in (0, -1, float('nan'), float('inf'), 3601, 'bad'):
            self.assertIn('error', jobs.start('echo no', timeout))
        self.assertIn('error', jobs.status([]))
        self.assertIn('error', await jobs.cancel(None))
        self.assertIn('error', await jobs.execute('bash', {'command': 'sleep 60', 'background': 'true'}))
        self.assertIn('error', await jobs.execute('job_cancel', {}))
        await jobs.close()

    async def test_conversation_and_interruption_do_not_cancel_background_work(self):
        with tempfile.TemporaryDirectory() as folder:
            session = SessionReport('realtime', 'test', {})
            reasoner = SharedReasoner(folder, model='test', api_key='test')
            command = 'echo $$ > pid; echo ready; sleep 60'
            def respond(messages):
                last = messages[-1]
                if last['role'] == 'tool':
                    return 'Started job-1 in the background.', [], {'cost': .001}
                if last['content'] == 'Start background work':
                    return '', [{'id': 'bg', 'name': 'bash', 'arguments': {
                        'command': command, 'background': True}}], {'cost': .001}
                return 'Twelve.', [], {'cost': .001}
            try:
                with patch.object(reasoner, '_request', side_effect=respond):
                    await reasoner.ask('Start background work', session)
                    await wait_output(reasoner.jobs, 'job-1', 'ready')
                    session.next_input()
                    self.assertEqual(await reasoner.ask('What is seven plus five?', session), 'Twelve.')
                    self.assertEqual(reasoner.jobs.status('job-1')['status'], 'running')
                    self.assertFalse(any('Current managed job states:' in str(m) for m in reasoner.history))
                    # Foreground cancellation is independent of the job manager.
                    session.next_input()
                    with patch.object(reasoner, '_request', return_value=('', [{'id': 'fg', 'name': 'bash',
                            'arguments': {'command': 'echo ready > fg; sleep 60'}}], {'cost': .001})):
                        task = asyncio.create_task(reasoner.ask('Foreground task', session))
                        async def ready():
                            while not (Path(folder)/'fg').exists():
                                await asyncio.sleep(.01)
                        await asyncio.wait_for(ready(), 3)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertEqual(reasoner.jobs.status('job-1')['status'], 'running')
            finally:
                await reasoner.close(session)
            self.assertEqual(session.background_jobs[0]['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
