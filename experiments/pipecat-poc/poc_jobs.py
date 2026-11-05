"""Session-owned background bash jobs. One active job; no restart persistence.

Runs under the same trusted-user permissions as foreground bash, not a sandbox.
Conversation cancellation never owns these tasks; explicit cancel/session close
kills their process groups. Output is a bounded tail, not an unlimited log.
"""
import asyncio
import copy
import math
import os
import time

from poc_tools import repair_tool_args, run_tool_async

BACKGROUND_START_TOOL = {'type': 'function', 'function': {
    'name': 'background_start',
    'description': 'Start long-running or explicitly backgrounded bash/Pi work and return a job ID immediately. New conversation does not cancel it. One active job; session-owned and time-bounded.',
    'parameters': {'type': 'object', 'properties': {
        'command': {'type': 'string'},
        'timeout_seconds': {'type': 'number', 'description': 'Positive deadline up to 3600 seconds; default 600.'}},
        'required': ['command']}}}

JOB_STATUS_TOOL = {'type': 'function', 'function': {
    'name': 'job_status',
    'description': 'Inspect a background job and its recent output, or list recent jobs. Does not wait for completion.',
    'parameters': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}}}}
JOB_CANCEL_TOOL = {'type': 'function', 'function': {
    'name': 'job_cancel', 'description': 'Explicitly stop a background job and its bash children.',
    'parameters': {'type': 'object', 'properties': {'job_id': {'type': 'string'}},
                   'required': ['job_id']}}}


class JobManager:
    def __init__(self, workdir, *, default_timeout=None, notify=None, clock=time.monotonic):
        self.workdir, self.clock, self.notify = workdir, clock, notify
        self.default_timeout = float(default_timeout if default_timeout is not None else
                                     os.getenv('CHAT_JOB_TIMEOUT', '600'))
        self._validate_timeout(self.default_timeout)
        self.jobs, self.tasks = {}, {}
        self._next_id, self.closed = 1, False

    @staticmethod
    def _validate_timeout(value):
        if not math.isfinite(value) or not 0 < value <= 3600:
            raise ValueError('Background timeout must be positive and at most 3600 seconds')

    def start(self, command, timeout=None):
        """Atomic acceptance: the task is owned here even if speech is interrupted."""
        if self.closed:
            return {'error': 'Background job manager is closed'}
        if any(not t.done() for t in self.tasks.values()):
            return {'error': 'One background job is already active. Check it or explicitly cancel it first.'}
        if not isinstance(command, str) or not command.strip():
            return {'error': 'A nonempty bash command is required'}
        try:
            timeout = float(self.default_timeout if timeout is None else timeout)
            self._validate_timeout(timeout)
        except (ValueError, TypeError) as exc:
            return {'error': str(exc)}
        job_id = f'job-{self._next_id}'
        self._next_id += 1
        job = {'job_id': job_id, 'command': command, 'status': 'queued',
               'timeout_s': timeout, 'started_at': None, 'finished_at': None,
               'output': '', 'result': None}
        # Keep at most twenty completed/active records in memory.
        while len(self.jobs) >= 20:
            del self.jobs[next(iter(self.jobs))]
        self.jobs[job_id] = job
        task = asyncio.create_task(self._run(job))
        self.tasks[job_id] = task
        def finished(done):
            self.tasks.pop(job_id, None)
            if done.cancelled() and job['status'] == 'queued':
                job.update(status='cancelled', finished_at=self.clock(),
                           result={'error': 'Cancelled before start'})
                self._announce(job)
        task.add_done_callback(finished)
        return copy.deepcopy(job)

    async def _run(self, job):
        job['status'], job['started_at'] = 'running', self.clock()
        try:
            result = await run_tool_async('bash', {'command': job['command']}, self.workdir,
                timeout=job['timeout_s'], on_output=lambda text: job.update(output=text))
            job['result'] = result
            job['status'] = ('timed_out' if result.get('timed_out') else
                             'failed' if result.get('error') or result.get('returncode', 0) else 'completed')
        except asyncio.CancelledError:
            job['status'], job['result'] = 'cancelled', {'error': 'Explicit cancellation or session shutdown'}
            raise
        except Exception as exc:
            job['status'], job['result'] = 'failed', {'error': f'{type(exc).__name__}: {exc}'}
        finally:
            job['finished_at'] = self.clock()
            self._announce(job)

    def _announce(self, job):
        if self.notify:
            try:
                self.notify(copy.deepcopy(job))
            except Exception as exc:
                job['notification_error'] = type(exc).__name__

    def status(self, job_id=None):
        if job_id is not None and not isinstance(job_id, str):
            return {'error': 'job_id must be a string'}
        if job_id is not None:
            return copy.deepcopy(self.jobs.get(job_id, {'error': 'Unknown job ID'}))
        return {'jobs': copy.deepcopy(list(self.jobs.values()))}

    async def cancel(self, job_id):
        if not isinstance(job_id, str) or job_id not in self.jobs:
            return {'error': 'Unknown job ID'}
        task = self.tasks.get(job_id)
        if task and not task.done():
            if not task.cancelling():
                task.cancel()
            # A job cancelled before its coroutine starts has no finally block.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            if self.jobs[job_id]['status'] == 'queued':
                self.jobs[job_id].update(status='cancelled', finished_at=self.clock(),
                    result={'error': 'Cancelled before start'})
        return self.status(job_id)

    async def execute(self, name, arguments):
        args = repair_tool_args(arguments)
        if name == 'background_start':
            return self.start(args.get('command'), args.get('timeout_seconds'))
        if name == 'job_status':
            return self.status(args.get('job_id'))
        if name == 'job_cancel':
            return await self.cancel(args.get('job_id'))
        if name == 'bash' and 'background' in args and not isinstance(args['background'], bool):
            return {'error': 'background must be a boolean'}
        if name == 'bash' and args.get('background') is True:
            return self.start(args.get('command'), args.get('timeout_seconds'))
        return await run_tool_async(name, args, self.workdir)

    async def close(self):
        self.closed = True
        for job_id in list(self.tasks):
            await self.cancel(job_id)
