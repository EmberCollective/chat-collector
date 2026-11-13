import readline from 'node:readline';
import {runPi} from './runtime.mjs';

const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
const pending = new Map();
let started = false;
let nextId = 0;
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
const requestTool = (name, args, signal) => new Promise((resolve, reject) => {
  const id = ++nextId;
  const abort = () => { pending.delete(id); reject(new Error('Tool request aborted')); };
  if (signal?.aborted) {abort(); return;}
  signal?.addEventListener('abort', abort, {once: true});
  pending.set(id, {resolve: value => {signal?.removeEventListener('abort', abort); resolve(value);},
    reject: error => {signal?.removeEventListener('abort', abort); reject(error);}});
  send({type: 'tool_request', id, name, arguments: args});
});
const safeCodes = new Set(['invalid_input', 'extension_load_failed', 'extension_error', 'tool_isolation_failed',
  'turn_budget', 'tool_budget', 'context_budget', 'compaction_failed', 'unknown_tool', 'invalid_source', 'invalid_task', 'timeout', 'model_error', 'missing_submission']);

lines.on('line', line => {
  let message;
  try { message = JSON.parse(line); } catch { send({type: 'error', code: 'protocol_error'}); process.exitCode = 1; lines.close(); return; }
  if (!started) {
    started = true;
    void runPi(message, (args, signal) => requestTool('read_attachment', args, signal),
      event => send({type: 'progress', event}),
      (args, signal) => requestTool('find_related_tasks', args, signal),
      (args, signal) => requestTool('validate_submission', args, signal)).then(result => send({type: 'result', result})).catch(error => {
      send({type: 'error', code: safeCodes.has(error.message) ? error.message : 'runtime_error'});
      process.exitCode = 1;
    }).finally(() => {lines.close(); process.stdin.pause();});
  } else if (message.type === 'tool_result' && pending.has(message.id)) {
    const waiter = pending.get(message.id); pending.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error));
    else waiter.resolve(message.result);
  }
});
lines.on('close', () => { if (!started) process.exitCode = 1; });
