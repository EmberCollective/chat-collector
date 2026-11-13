"""Transport-only bridge to Pi's native agent loop; no model loop lives in Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid


RUNTIME = Path(__file__).parent / 'pi-runtime'
ERRORS = {
    'timeout': 'Pi processing exceeded the configured time budget',
    'turn_budget': 'Pi processing exceeded agent.max_turns',
    'tool_budget': 'Pi processing exceeded agent.max_tool_calls',
    'context_budget': '初始消息批次超过 processing.max_input_chars；请缩小选取范围或提高初始批次预算。工具历史由 Pi 原生压缩管理',
    'compaction_failed': 'Pi native context compaction failed; no group result was accepted',
    'invalid_source': 'Pi rejected an unknown source reference',
    'invalid_task': 'Pi rejected an unknown or duplicate task reference',
    'unknown_tool': 'Pi attempted an unavailable tool; the group was not accepted',
    'missing_submission': 'The model ended without calling submit_tasks',
    'model_error': 'Pi model request failed; check endpoint, model and credentials',
}


def runtime_status():
    portable = RUNTIME / 'node' / ('node.exe' if os.name == 'nt' else 'bin/node')
    executable = str(portable) if portable.is_file() else shutil.which('node')
    package = RUNTIME / 'node_modules' / '@earendil-works' / 'pi-coding-agent' / 'package.json'
    return {'installed': bool(executable and package.is_file()), 'node': executable,
            'package': '@earendil-works/pi-coding-agent', 'version': '1.5.2'}


def run_agent(config, system, payload, *, api_key='', attachment_reader=None, existing_tasks=None, task_finder=None, submission_validator=None):
    runtime = runtime_status()
    if not runtime['installed']:
        raise RuntimeError('Pi runtime is missing; install the pinned packages with npm ci in processor/pi-runtime and Node >=22.19')
    model = {name: config['model'][name] for name in ('base_url', 'model', 'timeout_seconds', 'vision', 'context_window', 'max_output_tokens') if name in config['model']}
    model['timeout_seconds'] = model.get('timeout_seconds') or 120
    model['vision'] = bool(model.get('vision'))
    if not isinstance(api_key, str) or (api_key and (not api_key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in api_key))):
        raise ValueError('Model credential must be a single ASCII token')
    request = {'model': model, 'agent': config.get('agent', {}), 'api_key': api_key,
               'processing': {'max_input_chars': config.get('processing', {}).get('max_input_chars', 60000)},
               'system': system, 'payload': payload, 'existing_tasks': existing_tasks or []}
    incoming = queue.Queue()
    progress_file = Path(config['_progress_file']) if config.get('_progress_file') else None
    run_id = uuid.uuid4().hex
    environment = {**os.environ, 'PI_OFFLINE': '1'}
    environment.pop(config['model'].get('api_key_env', ''), None)
    process = subprocess.Popen([runtime['node'], str(RUNTIME / 'runner.mjs')], cwd=RUNTIME,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', bufsize=1,
        env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + model['timeout_seconds'] + 5

    def receive():
        try:
            while line := process.stdout.readline(32 * 1024 * 1024 + 1):
                if len(line) > 32 * 1024 * 1024:
                    incoming.put(('error', None)); return
                incoming.put(('message', json.loads(line)))
        except (ValueError, OSError):
            incoming.put(('error', None))
        finally:
            incoming.put(('closed', None))

    def execute_tool(message):
        try:
            if message['name'] == 'validate_submission':
                result = submission_validator(**message['arguments']) if submission_validator else {'accepted': True}
                incoming.put(('reply', {'type': 'tool_result', 'id': message['id'], 'result': result}))
                return
            handler = attachment_reader if message['name'] == 'read_attachment' else task_finder
            if handler is None:
                raise RuntimeError('Tool unavailable')
            result = handler(**message['arguments'])
            incoming.put(('reply', {'type': 'tool_result', 'id': message['id'], 'result': result}))
        except Exception as exc:
            # Third-party parser errors can contain local paths or private contents.
            error = {'read_attachment': 'attachment_read_failed', 'find_related_tasks': 'task_search_failed',
                     'validate_submission': 'submission_validation_failed'}[message['name']]
            if message['name'] == 'find_related_tasks' and isinstance(exc, ValueError) and len(str(exc))<=150 and str(exc).replace('_','').isalnum():
                error = str(exc)
            incoming.put(('reply', {'type': 'tool_result', 'id': message['id'], 'error': error}))

    def send(value):
        process.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
        process.stdin.flush()

    threading.Thread(target=receive, daemon=True).start()
    try:
        send(request)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(ERRORS['timeout'])
            try:
                kind, message = incoming.get(timeout=remaining)
            except queue.Empty:
                raise RuntimeError(ERRORS['timeout']) from None
            if kind in {'error', 'closed'}:
                raise RuntimeError('Pi runtime stopped unexpectedly; verify Node >=22.19 and the pinned npm installation')
            if kind == 'reply':
                send(message)
            elif message.get('type') == 'tool_request':
                if message.get('name') not in {'read_attachment', 'find_related_tasks', 'validate_submission'} or not isinstance(message.get('arguments'), dict):
                    raise RuntimeError('Invalid Pi tool request')
                threading.Thread(target=execute_tool, args=(message,), daemon=True).start()
            elif message.get('type') == 'progress':
                event = message.get('event')
                if not isinstance(event, dict):
                    raise RuntimeError('Invalid Pi progress event')
                if progress_file:
                    safe = {key: value for key, value in event.items() if key in {
                        'time', 'type', 'turn', 'tool', 'call_id', 'ref', 'pages', 'failed', 'attempt',
                        'chars', 'context_window', 'reserve_tokens', 'code', 'turns', 'tool_calls', 'compactions', 'retries', 'status', 'warnings'}}
                    progress_file.parent.mkdir(parents=True, exist_ok=True)
                    with progress_file.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(dict(safe, run_id=run_id), ensure_ascii=False)+'\n')
            elif message.get('type') == 'error':
                raise RuntimeError(ERRORS.get(message.get('code'), 'Pi processing failed; no group result was accepted'))
            elif message.get('type') == 'result':
                result = message.get('result')
                if not isinstance(result, dict) or not isinstance(result.get('tasks'), list):
                    raise RuntimeError('Pi returned an invalid task submission')
                return result
            else:
                raise RuntimeError('Invalid Pi transport response')
    except (BrokenPipeError, OSError):
        raise RuntimeError('Pi runtime transport failed') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
