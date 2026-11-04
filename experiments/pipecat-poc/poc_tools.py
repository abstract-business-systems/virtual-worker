"""Shared POC tools. Bash runs as the current user: NOT a security sandbox.
Only use trusted working folders and authorized test instructions.
"""
import json
import os
import signal
import subprocess


def repair_tool_args(arguments):
    if isinstance(arguments, dict):
        return arguments
    decoder, offset, last = json.JSONDecoder(), 0, {}
    source = arguments or '{}'
    while offset < len(source):
        offset += len(source[offset:])-len(source[offset:].lstrip())
        if offset == len(source):
            break
        try:
            obj, offset = decoder.raw_decode(source, offset)
        except json.JSONDecodeError as exc:
            raise ValueError('Tool arguments are not complete JSON objects') from exc
        if not isinstance(obj, dict):
            raise ValueError('Tool arguments must be a JSON object')
        if obj:
            last = obj
    return last


def run_tool(name, arguments, workdir):
    try:
        args = repair_tool_args(arguments)
    except ValueError as exc:
        return {'error': str(exc)}
    if name == 'get_meeting_context':
        return {'stub': True, 'recent_meeting': 'M7 POC scoping',
                'next_event': 'Friday demo review'}
    if name != 'bash':
        return {'error': f'unknown tool {name}'}
    cmd = args.get('command')
    if not isinstance(cmd, str) or not cmd.strip():
        return {'returncode': -1, 'output': '(empty command)'}
    try:
        proc = subprocess.Popen(['bash', '-c', cmd], cwd=workdir,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return {'returncode': -1, 'output': '(command timed out after 60s)'}
        return {'returncode': proc.returncode,
                'output': output.strip()[:4000] or '(no output)'}
    except OSError as exc:
        return {'returncode': -1, 'output': str(exc)}


async def tool_rounds(messages, request, execute, *, max_rounds=4):
    """Bounded standard text tool loop. Accounts for EVERY request via request.

    request(messages) returns (text, calls); execute(name,args) is async.
    Every call in a response is answered; IDs and JSON are preserved/repaired.
    """
    import uuid
    for _ in range(max_rounds):
        text, calls = await request(messages)
        calls = [dict(c) for c in calls if c.get('name')]
        if not calls:
            if not text.strip():
                raise RuntimeError('Model returned no answer')
            return text.strip()
        for c in calls:
            c['id'] = c.get('id') or 'call_' + uuid.uuid4().hex
            c['arguments'] = json.dumps(repair_tool_args(c.get('arguments')))
        messages.append({'role': 'assistant', 'content': text or None, 'tool_calls': [
            {'id': c['id'], 'type': 'function', 'function': {
                'name': c['name'], 'arguments': c['arguments']}} for c in calls]})
        for c in calls:
            result = await execute(c['name'], c['arguments'])
            messages.append({'role': 'tool', 'tool_call_id': c['id'],
                             'content': json.dumps(result)})
    raise RuntimeError(f'Tool loop exceeded {max_rounds} model requests')
