"""Opt-in, read-only bridge to Friday's existing ACL-checked context service.

Only for a trusted operator CLI. It is NOT speaker authentication and does not
replace the meeting worker or enable its command/admin protocol to the model.
"""
import asyncio
import json
import os
import uuid

WORKER_CONTEXT_TOOL = {'type': 'function', 'function': {
    'name': 'worker_context',
    'description': 'Read relevant meeting/knowledge context from Friday, under the configured operator identity and existing backend ACL. Report unavailable/denied context honestly.',
    'parameters': {'type': 'object', 'properties': {'question': {'type': 'string'}},
                   'required': ['question']}}}


class WorkerBridge:
    def __init__(self, socket_path, requester, *, timeout=5):
        if not socket_path or not requester:
            raise ValueError('Worker bridge requires both socket and operator identity')
        self.socket_path, self.requester, self.timeout = socket_path, requester, timeout

    @classmethod
    def from_environment(cls):
        path, requester = os.getenv('CHAT_WORKER_SOCKET'), os.getenv('CHAT_WORKER_REQUESTER')
        if not path and not requester:
            return None
        return cls(path, requester)

    async def context(self, question):
        if not isinstance(question, str) or not question.strip():
            return {'error': 'A nonempty question is required'}
        async def request():
            reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=65536)
            try:
                request_id = uuid.uuid4().hex
                # The model cannot change requester, in_meeting, ACL or operation.
                msg = {'type': 'context.get', 'req_id': request_id,
                       'requester': self.requester, 'question': question, 'in_meeting': False}
                writer.write((json.dumps(msg)+'\n').encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                if response.get('req_id') != request_id:
                    return {'error': 'Worker response ID mismatch'}
                if not response.get('ok'):
                    return {'error': 'Worker context unavailable or denied'}
                return response.get('data', {})
            finally:
                writer.close()
                await writer.wait_closed()
        try:
            return await asyncio.wait_for(request(), self.timeout)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            return {'error': f'Worker context unavailable: {type(exc).__name__}'}
