import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';
import {runPi} from '../runtime.mjs';

export function toolStream(response, name, args, id = 'call_fixture') {
  response.writeHead(200, {'Content-Type': 'text/event-stream'});
  const frame = (delta, finish_reason = null) => 'data: ' + JSON.stringify({id: 'chatcmpl-fixture',
    object: 'chat.completion.chunk', created: 1, model: 'fixture',
    choices: [{index: 0, delta, finish_reason}]}) + '\n\n';
  response.write(frame({role: 'assistant', tool_calls: [{index: 0, id, type: 'function', function: {name, arguments: JSON.stringify(args)}}]}));
  response.write(frame({}, 'tool_calls'));
  response.end('data: [DONE]\n\n');
}

function textStream(response, text) {
  response.writeHead(200, {'Content-Type': 'text/event-stream'});
  for (const [delta, reason] of [[{role: 'assistant', content: text}, null], [{}, 'stop']]) {
    response.write('data: ' + JSON.stringify({id: 'summary-fixture', object: 'chat.completion.chunk', created: 1, model: 'fixture',
      choices: [{index: 0, delta, finish_reason: reason}]}) + '\n\n');
  }
  response.end('data: [DONE]\n\n');
}

const task = sources => ({task_id: null, title: 'Submit report', event: 'Submit report tomorrow', priority: 'P2',
  priority_reason: 'Explicit requirement', status: 'open',
  time: {start: null, deadline: '2026-11-13', text: 'tomorrow'}, people: [], location: null,
  needs_review: true, review_reasons: ['Owner unknown'], sources});

async function fixture(responder, execute) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const parsed = JSON.parse(body);
    requests.push(parsed);
    responder(res, parsed, requests.length);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const input = {model: {base_url: `http://127.0.0.1:${server.address().port}/v1`, model: 'fixture', timeout_seconds: 15, vision: false},
    agent: {max_turns: 8, max_tool_calls: 12}, api_key: 'synthetic-private-key', system: 'Follow local task rules. Use tools.',
    payload: {segments: [{id: 's1', messages: [{id: 'm1', text: 'See attached report', sender: 'sender1', time: '2026-11-12T10:00:00+08:00', attachments: ['f1']}]}],
      attachments: [{id: 'f1', name: 'report.pdf', page_count: 2}]}, existing_tasks: []};
  try { return await execute(input, requests); }
  finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
}

test('real Pi SDK performs attachment and lookup tools then native final submission', async () => {
  await fixture((res, _body, index) => {
    if (index === 1) toolStream(res, 'read_attachment', {ref: 'f1', pages: '2'});
    else if (index === 2) toolStream(res, 'find_related_tasks', {query: 'report'});
    else toolStream(res, 'submit_tasks', {tasks: [task(['m1', 'f1:p2'])]});
  }, async (input, requests) => {
    const calls = [];
    const result = await runPi(input, async args => {
      calls.push(args);
      return {content: [{type: 'text', text: '[f1:p2] Submit report tomorrow.'}], details: {evidence: [{ref: 'f1:p2', page: 2}]}};
    }, () => {}, async args => {
      assert.deepEqual(args,{query:'report'});
      return {tasks:[],has_more:false,next_offset:null};
    });
    assert.equal(result.tasks.length, 1);
    assert.equal(result._agent.runtime, 'pi-coding-agent');
    assert.equal(result._agent.version, '1.5.2');
    assert.equal(result._agent.turns, 3);
    assert.equal(result._agent.tool_calls, 3);
    assert.equal(requests.length, 3, 'submit_tasks terminates without another model request');
    assert.deepEqual(calls, [{ref: 'f1', pages: '2'}]);
    assert.deepEqual(requests[0].tools.map(t => t.function.name).sort(), ['find_related_tasks', 'read_attachment', 'submit_tasks']);
    assert.ok(JSON.stringify(requests[1].messages).includes('f1:p2'));
    assert.ok(!JSON.stringify(requests).includes('synthetic-private-key'));
    assert.ok(!JSON.stringify(requests).includes('/pi-runtime'), 'Pi must not append its local runtime path to the prompt');
  });
});

test('unknown native tool fails closed', async () => {
  await fixture(res => toolStream(res, 'bash', {command: 'echo forbidden'}), async input => {
    await assert.rejects(runPi(input, () => assert.fail('no attachment read')), /unknown_tool/);
  });
});

test('unavailable attachment is visible as a tool error but empty submission is allowed', async () => {
  await fixture((res,body,n) => n===1 ? toolStream(res,'read_attachment',{ref:'f1'})
    : toolStream(res,'submit_tasks',{tasks:[],attachment_decisions:[{ref:'f1',reason:'Not required for the visible message'}]}), async input => {
    const events=[];
    const result=await runPi(input,async()=>({content:[{type:'text',text:'Local file unavailable'}],
      details:{status:'needs_review',warnings:['local_source_not_found'],evidence:[]}}),e=>events.push(e));
    assert.deepEqual(result.tasks,[]);
    assert.equal(result.attachment_decisions[0].ref,'f1');
    assert.ok(events.some(e=>e.type==='tool_execution_end' && e.tool==='read_attachment' && e.failed));
  });
});

test('unread attachment evidence cannot be submitted', async () => {
  await fixture(res => toolStream(res, 'submit_tasks', {tasks: [task(['f1:p2'])]}), async input => {
    await assert.rejects(runPi(input, () => assert.fail('no attachment read')), /invalid_source/);
  });
});

test('tool-call budget stops Pi native continuation', async () => {
  await fixture(res => toolStream(res, 'find_related_tasks', {}), async (input, requests) => {
    input.agent.max_tool_calls = 1;
    await assert.rejects(runPi(input, () => assert.fail('no attachment read')), /tool_budget/);
    assert.equal(requests.length, 2);
  });
});

test('turn budget stops repeated tool reasoning', async () => {
  await fixture(res => toolStream(res, 'find_related_tasks', {}), async (input, requests) => {
    input.agent.max_turns = 1;
    await assert.rejects(runPi(input, () => assert.fail('no attachment read')), /turn_budget/);
    assert.ok(requests.length <= 2);
  });
});

test('missing attachment ref never reaches the reader', async () => {
  await fixture((res, _body, index) => toolStream(res, index === 1 ? 'read_attachment' : 'submit_tasks',
    index === 1 ? {ref: '../../private'} : {tasks: []}), async input => {
    const result = await runPi(input, () => assert.fail('unknown attachment must not reach Python'));
    assert.deepEqual(result.tasks, []);
  });
});

test('whole-run timeout aborts an unresponsive compatible endpoint', async () => {
  await fixture(() => {}, async input => {
    input.model.timeout_seconds = 1;
    await assert.rejects(runPi(input, () => assert.fail()), /timeout/);
  });
});

test('endpoint failure does not expose server response or credential', async () => {
  await fixture(res => {res.writeHead(401); res.end('synthetic-private-key and private server contents');}, async input => {
    await assert.rejects(runPi(input, () => assert.fail()), error => error.message === 'model_error');
  });
});

test('multi-conversation payload retains source refs and filters related tasks', async () => {
  await fixture((res, _body, index) => {
    if (index === 1) toolStream(res, 'find_related_tasks', {conversation_ref: 'c2'});
    else toolStream(res, 'submit_tasks', {tasks: [{...task(['m2']), task_id: 't2'}]});
  }, async (input, requests) => {
    delete input.payload.segments;
    input.payload.conversations = [{id: 'c1', segments: [{messages: [{id: 'm1', text: 'One'}]}]},
      {id: 'c2', segments: [{messages: [{id: 'm2', text: 'Two'}]}]}];
    input.existing_tasks = [{task_id: 't1', title: 'One', event: 'One', conversation_ref: 'c1'},
      {task_id: 't2', title: 'Two', event: 'Two', conversation_ref: 'c2'}];
    const result = await runPi(input, () => assert.fail(), () => {}, async args => {
      assert.deepEqual(args, {conversation_ref:'c2'});
      return {tasks:[{...input.existing_tasks[1],updatable:true}],has_more:false,next_offset:null};
    });
    assert.equal(result.tasks[0].task_id, 't2');
    const toolMessage = requests[1].messages.find(message => message.role === 'tool');
    assert.ok(JSON.stringify(toolMessage).includes('t2'));
    assert.ok(!JSON.stringify(toolMessage).includes('t1'));
  });
});

test('live database results can register new tasks but superseded results cannot be updated', async () => {
  for (const updatable of [true,false]) {
    await fixture((res,body,index) => index===1
      ? toolStream(res,'find_related_tasks',{platforms:['wechat'],people:['张老师'],offset:40})
      : toolStream(res,'submit_tasks',{tasks:[{...task(['m1']),task_id:'t88'}]}), async input => {
      const run=runPi(input,()=>assert.fail(),()=>{},async args=>{
        assert.equal(args.offset,40);
        return {tasks:[{task_id:'t88',title:'Old task',updatable}],has_more:true,next_offset:60};
      });
      if(updatable)assert.equal((await run).tasks[0].task_id,'t88');
      else await assert.rejects(run,/invalid_task/);
    });
  }
});

test('vision is explicit and native Pi carries the allowed image to the next request', async () => {
  const pixel = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1kAAAAASUVORK5CYII=';
  await fixture((res, _body, index) => index === 1
    ? toolStream(res, 'read_attachment', {ref: 'f1', pages: '1'})
    : toolStream(res, 'submit_tasks', {tasks: [task(['m1', 'f1:p1'])]}), async (input, requests) => {
    input.model.vision = true;
    const result = await runPi(input, async () => ({content: [{type: 'text', text: '[f1:p1] page'},
      {type: 'image', data: pixel, mimeType: 'image/png'}], details: {evidence: [{ref: 'f1:p1', page: 1}]}}));
    assert.equal(result.tasks.length, 1);
    assert.ok(JSON.stringify(requests[1].messages).includes('data:image/png;base64,' + pixel));
  });
});

test('native Pi compaction summarizes long tool context and evidence survives', async () => {
  let summaries = 0;
  let reads = 0;
  await fixture((res, body) => {
    if (!body.tools?.length) {summaries++; textStream(res, 'Read f1:p1: submit report tomorrow. Message m1 is the source. Finish with submit_tasks.');}
    else if (!reads++) toolStream(res, 'read_attachment', {ref: 'f1', pages: '1'});
    else toolStream(res, 'submit_tasks', {tasks: [task(['m1', 'f1:p1'])]});
  }, async input => {
    input.model.context_window = 4096;
    input.model.max_output_tokens = 512;
    Object.assign(input.agent, {compaction_reserve_tokens: 1024, compaction_keep_recent_tokens: 128});
    input.processing = {max_input_chars: 100000};
    const events = [];
    const result = await runPi(input, async () => ({content: [{type: 'text', text: '[f1:p1] ' + 'Report evidence. '.repeat(1100)}],
      details: {evidence: [{ref: 'f1:p1', page: 1}]}}), e => events.push(e));
    assert.ok(summaries >= 1, 'Pi must make its native summarization request');
    assert.ok(result._agent.compactions >= 1);
    assert.deepEqual(result.tasks[0].sources, ['m1', 'f1:p1']);
    assert.ok(events.some(e => e.type === 'compaction_start'));
    assert.ok(events.some(e => e.type === 'compaction_end' && !e.failed));
  });
});

test('initial batch budget does not reject tool history inside the model window', async () => {
  await fixture((res, body, n) => n === 1 ? toolStream(res, 'read_attachment', {ref: 'f1'})
    : toolStream(res, 'submit_tasks', {tasks: [task(['f1:p1'])]}), async (input, requests) => {
    input.model.context_window = 1000000;
    input.processing = {max_input_chars: 60000};
    const events = [];
    const result = await runPi(input, async () => ({content: [{type: 'text', text: 'X'.repeat(80000)}],
      details: {evidence: [{ref: 'f1:p1', page: 1}]}}), e => events.push(e));
    assert.equal(result.tasks.length, 1);
    assert.equal(requests.length, 2);
    assert.ok(events.some(e => e.type === 'request_prepared' && e.chars > 60000));
    assert.equal(events.at(-1).type, 'run_end');
    assert.ok(!JSON.stringify(events).includes('X'.repeat(100)));
    assert.ok(!JSON.stringify(events).includes(input.api_key));
  });
});

test('initial batch limit still rejects oversized new input before a provider call', async () => {
  await fixture(() => assert.fail('No request expected'), async (input, requests) => {
    input.processing = {max_input_chars: 1000};
    input.payload.segments[0].messages[0].text = 'X'.repeat(2000);
    await assert.rejects(runPi(input, () => assert.fail()), /context_budget/);
    assert.equal(requests.length, 0);
  });
});

test('Pi native retry recovers a temporary model error', async () => {
  await fixture((res, _body, index) => {
    if (index === 1) {res.writeHead(503); res.end('temporarily unavailable');}
    else toolStream(res, 'submit_tasks', {tasks: []});
  }, async (input, requests) => {
    input.agent.max_retries = 1;
    const result = await runPi(input, () => assert.fail());
    assert.equal(result._agent.retries, 1);
    assert.equal(requests.length, 2);
  });
});

test('failed native compaction rejects the group without accepting a submission', async () => {
  await fixture((res, body) => {
    if (!body.tools?.length) {res.writeHead(401); res.end('synthetic compaction failure');}
    else toolStream(res, 'read_attachment', {ref: 'f1', pages: '1'});
  }, async input => {
    input.model.context_window = 4096;
    input.model.max_output_tokens = 512;
    Object.assign(input.agent, {compaction_reserve_tokens: 1024, compaction_keep_recent_tokens: 128, max_retries: 0});
    input.processing = {max_input_chars: 100000};
    await assert.rejects(runPi(input, async () => ({content: [{type: 'text', text: 'Long evidence. '.repeat(1300)}],
      details: {evidence: [{ref: 'f1:p1', page: 1}]}})), /compaction_failed/);
  });
});
