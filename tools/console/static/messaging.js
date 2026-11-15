'use strict';
// Plans never prepare assets or enqueue; writes require an explicit task/material action.
document.addEventListener('DOMContentLoaded', () => {
  const $ = id => document.getElementById('delivery-' + id);
  const notice = document.getElementById('messaging-notice');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {queued:'已排队',waiting_import:'等待接收',scheduled:'等待提醒',snoozed:'已延后',unknown:'结果未知',failed:'投递失败',feedback:'等待回写',sending:'正在投递',retry:'等待重试',disabled:'未启用',synced:'已同步',ended:'不再提醒',no_reminders:'无后续提醒',service_stopped:'后台未运行',service_unknown:'后台状态未确认',paused:'投递任务已暂停'};
  let data = null, digest = null, offset = 0, loading = false, sequence = 0, digestSequence = 0, receivedAt = 0, clockBase = 0, detailTask = null, busy = false;
  const timeNow = () => clockBase ? clockBase + (Date.now() - receivedAt) / 1000 : Date.now() / 1000;
  const seconds = value => typeof value === 'number' ? value : Date.parse(value) / 1000;
  function date(value, zone = 'Asia/Shanghai') {
    if (value == null || !Number.isFinite(seconds(value))) return '—';
    const stamp = new Date(seconds(value) * 1000);
    return new Intl.DateTimeFormat('zh-CN', {timeZone:zone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(stamp.getUTCSeconds() ? {second:'2-digit'} : {}),hour12:false}).format(stamp);
  }
  const duration = value => value % 86400 === 0 ? value / 86400 + ' 天' : value % 3600 === 0 ? value / 3600 + ' 小时' : value % 60 === 0 ? value / 60 + ' 分钟' : value + ' 秒';
  function remaining(value) {
    const delta = Math.ceil(seconds(value) - timeNow());
    if (delta <= 0) return '已到计划时间';
    if (delta < 60) return delta + ' 秒后';
    const minutes = Math.ceil(delta / 60);
    if (minutes < 60) return minutes + ' 分钟后';
    return Math.floor(minutes / 60) + ' 小时' + (minutes % 60 ? ' ' + minutes % 60 + ' 分钟' : '') + '后';
  }
  const priority = task => `<span class="priority ${['P0','P1'].includes(task.priority) ? 'high' : ''}">${esc(task.priority)}</span>`;
  const timer = value => `<span data-delivery-time="${esc(value)}">${esc(remaining(value))}</span>`;
  async function request(path, options = {}) {
    const session = await window.consoleSession();
    const response = await fetch('/api/messaging' + path, {...options,headers:{'X-Console-Token':session.token,'Content-Type':'application/json'}});
    if (!response.ok) throw Error((await response.json()).detail || '读取失败，请稍后重试');
    return response;
  }
  function options(select, targets, empty = false) {
    const signature = JSON.stringify(targets);
    if (select.dataset.options === signature) return;
    const selected = select.value;select.replaceChildren();
    if (empty) select.add(new Option('全部目标',''));
    for (const t of targets) select.add(new Option(t.name + (t.enabled ? '' : '（未启用）'),t.id));
    if ([...select.options].some(o => o.value === selected)) select.value = selected;
    select.dataset.options = signature;
  }
  function displayPlan(plan) {
    const runtime = data?.runtime;
    if (plan.due_at == null || !runtime) return plan;
    if (runtime.state !== 'running') return {...plan,due_at:null,state:runtime.state === 'paused' ? 'paused' : ['stopped','disabled','stopping'].includes(runtime.state) ? 'service_stopped' : 'service_unknown',reason:'规则计划 '+date(plan.due_at)+'；'+plan.reason};
    return {...plan,due_at:Math.max(plan.due_at,runtime.next_check_at || 0)};
  }
  function rowInfo(task) {
    const plans = task.plans.filter(p => !$('target-filter').value || p.target_id === $('target-filter').value).map(displayPlan);
    const problem = plans.find(p => ['unknown','feedback','failed'].includes(p.state));
    const next = plans.filter(p => p.due_at != null).sort((a,b) => a.due_at - b.due_at)[0];
    return {plans,chosen:problem || next || plans[0],next,problem};
  }
  function renderTasks() {
    const active = document.activeElement?.dataset.taskId;
    const signature = JSON.stringify({tasks:data.tasks,display:data.tasks.map(rowInfo),targets:data.targets});
    const html = $('task-rows').dataset.content === signature ? null : data.tasks.map(task => {
      const {plans,chosen:p,next,problem} = rowInfo(task);
      const tone = problem ? p.state === 'feedback' ? 'warn' : 'error' : p?.state.startsWith('service_') || p?.state === 'paused' ? 'warn' : '';
      const selected = data.targets.filter(t => plans.some(p => p.target_id === t.id));
      const counts = selected.filter(t => t.mode === 'notify').length+' 个提醒目标 · '+selected.filter(t => t.mode === 'sync').length+' 个同步目标';
      return `<tr data-task="${esc(task.task_id)}"><td><div class="task-top">${priority(task)}<button class="task-title" data-task-id="${esc(task.task_id)}">${esc(task.title)} <span aria-hidden="true" class="muted">↗</span></button></div><span class="sub">${esc(task.source_names.join('、') || '未提供来源')} · ${task.source_count} 条原文 · ${esc(task.task_id)}</span>${task.updated_at ? `<span class="sub">更新于 ${esc(date(task.updated_at))}</span>` : ''}</td><td data-label="截止时间"><div>${task.deadline_at != null ? `<span class="date">${esc(date(task.deadline_at))}</span><span class="sub">${task.date_only ? '仅日期 · 按任务时区补足时刻' : task.status !== 'open' ? '已结束' : timer(task.deadline_at)}</span>` : '未提供<span class="sub">按普通间隔规则</span>'}</div></td><td data-label="下次投递"><div><span class="eta ${tone}">${esc(p?.due_at != null ? date(p.due_at) : labels[p?.state] || '未配置投递目标')}</span><span class="sub">${p?.due_at != null ? timer(p.due_at)+' · ' : ''}${esc(p?.reason || '请先配置并启用投递目标')}</span>${problem && next ? '<span class="sub">其他目标仍有投递计划，展开查看</span>' : ''}</div></td><td data-label="目标状态"><div><span class="badge ${tone || (next ? 'green' : '')}">${esc(labels[p?.state] || '未配置目标')}</span><span class="sub">${esc(selected.length === 1 ? selected[0].name : counts)}</span></div></td></tr>`;
    }).join('');
    if (html !== null) {
      $('task-rows').innerHTML = html;$('task-rows').dataset.content = signature;
      if (active) $('task-rows').querySelector(`[data-task-id="${CSS.escape(active)}"]`)?.focus({preventScroll:true});
    }
    $('task-count').textContent = (data.tasks.length ? `${offset+1}–${offset+data.tasks.length}` : '0')+' / '+data.matched_tasks+' 项';
    $('task-empty').hidden = data.tasks.length !== 0;$('more').hidden = !data.has_more;$('previous').hidden = offset === 0;
    $('issues').hidden = data.attention === 0;$('issue-count').textContent = data.attention+' 项需要处理';
  }
  function renderRuntime() {
    const r = data.runtime, names = {running:'投递后台运行中',stopped:'后台未运行',disabled:'自动投递任务未启用',paused:'投递任务已暂停',starting:'后台正在启动',stopping:'后台正在停止',unknown:'投递后台状态未确认',duplicate:'发现重复后台，请检查',config_changed:'后台配置待应用，请在自动化中查看'};
    $('runtime').textContent = !data.enabled ? '外部投递已关闭' : (names[r.state] || names.unknown)+(r.state === 'running' && r.interval_seconds ? ' · 每 '+duration(r.interval_seconds)+'检查' : '');
    $('updated').textContent = '更新于 '+date(data.generated_at)+' · 列表时间为上海时区';
    $('runtime').classList.toggle('muted',r.state !== 'running' || !data.enabled);
    if (r.state === 'running' && data.enabled) $('runtime').insertAdjacentHTML('afterbegin','<i class="status-dot" aria-hidden="true"></i>');
    document.getElementById('messaging-sync').textContent = data.pending_events ? `导入并保存本地材料（${data.pending_events} 项更新）` : '导入并保存本地材料';
  }
  async function load(reset = false) {
    if (reset) offset = 0;
    const seq = ++sequence;loading = true;
    const params = new URLSearchParams({limit:50,offset,search:$('task-search').value.trim(),state:$('state-filter').value});
    if ($('target-filter').value) params.set('target_id',$('target-filter').value);
    try {
      const result = await request('/schedule?'+params).then(r => r.json());
      if (seq !== sequence) return;
      data = result;receivedAt = Date.now();clockBase = data.generated_at;
      if (!data.tasks.length && offset > 0) {offset = 0;return load();}
      options($('target-filter'),data.targets,true);renderRuntime();renderTasks();
      if ($('digest-target').value && !data.targets.some(t => t.digest && t.id === $('digest-target').value)) $('digest-target').value = '';
      if (detailTask) renderDetailPlans();
      notice.textContent = '';
      if (!$('digest-view').hidden) await loadDigest();
    } catch (error) {if (seq === sequence) notice.textContent = '投递计划读取失败：'+error.message+'。保留上次结果，可点击刷新重试。';}
    finally {if (seq === sequence) loading = false;}
  }
  function renderDetailPlans() {
    const task = data?.tasks.find(t => t.task_id === detailTask?.task_id) || detailTask, body = $('detail-plans');
    if (!task || !body) return;
    body.innerHTML = task.plans.map(raw => {
      const p = displayPlan(raw), t = data?.targets.find(t => t.id === p.target_id);
      return `<div class="target-plan"><div class="target-plan-head"><strong>${esc(t?.name || p.target_id)} <span class="muted small">${esc(t?.description)}</span></strong><span class="badge ${['unknown','failed'].includes(p.state) ? 'error' : p.state === 'feedback' ? 'warn' : ''}">${esc(labels[p.state])}</span></div><p>${p.due_at != null ? `<span class="eta">预计 ${esc(date(p.due_at))} · ${timer(p.due_at)}</span>` : ''}${esc(p.reason)}</p>${p.last_sent_at ? `<span class="sub">最近成功${t?.mode === 'sync' ? '同步' : '通知'}：${esc(date(p.last_sent_at))}</span>` : ''}${['unknown','failed','feedback','retry'].includes(p.state) ? '<button class="text-btn small" data-delivery-recovery>查看投递与回写恢复 →</button>' : ''}${['service_stopped','service_unknown','paused','disabled'].includes(p.state) ? '<a class="small" href="#automation/channels">查看自动化与通道设置 →</a>' : ''}</div>`;
    }).join('') || '<p class="muted">尚未配置投递目标。</p>';
  }
  async function showTask(id) {
    detailTask = data?.tasks.find(t => t.task_id === id) || null;
    $('detail-title').textContent = detailTask?.title || '正在读取待办…';$('detail-id').textContent = id;$('detail-body').textContent = '正在读取行动与材料…';$('task-dialog').showModal();
    try {
      const task = await request('/tasks/'+encodeURIComponent(id)).then(r => r.json());
      if (!$('task-dialog').open || $('detail-id').textContent !== id) return;
      detailTask = task.delivery_plan;
      $('detail-title').textContent = task.title;
      const summary = detailTask, nodes = summary?.nodes || [];
      $('detail-body').innerHTML = `<dl class="detail-summary"><div><dt>截止时间</dt><dd>${esc(summary?.deadline_at ? date(summary.deadline_at) : task.time?.deadline || '未提供')}${summary?.date_only ? '<span class="sub">原文仅日期，时刻按任务时区补足</span>' : ''}</dd></div><div><dt>当前状态</dt><dd>${esc(({open:'待完成',completed:'已完成',done:'已完成',cancelled:'已取消',superseded:'已替代'})[task.status] || task.status)}</dd></div></dl><section class="detail-section"><h3>每个目标的投递计划</h3><div id="delivery-detail-plans"></div></section><section class="detail-section"><h3>截止提醒节点</h3>${nodes.length ? '<ol class="timeline">'+nodes.map(n => `<li><time>${esc(date(n.due_at))}</time><span>${esc(n.offset_seconds ? '提前 '+duration(n.offset_seconds) : '截止时')}<small>是否执行按各目标回执与当前状态判定</small></span></li>`).join('')+'</ol>' : '<p class="muted small">无截止节点时使用普通间隔；同步型目标只同步变更。</p>'}</section><section class="detail-section"><h3>行动与材料</h3><p>${esc(task.event)}</p><ol>${(task.action_steps || []).map(s => `<li>${esc(s)}</li>`).join('')}</ol><p class="muted small">参与人员：${esc((task.people || []).map(p => [p.name,p.role].filter(Boolean).join(' · ')).join('、') || '未明确')}；参与人员不等同于负责人。</p>${task.notes?.length ? '<p class="muted small">说明：'+task.notes.map(esc).join('；')+'</p>' : ''}<ul id="delivery-materials"></ul>${task.unselected_materials_count ? '<p class="muted small">另有 '+task.unselected_materials_count+' 个来源附件未被选为材料，不自动上传。</p>' : ''}<details><summary>信息来源与原文</summary>${(task.sources || []).map(s => `<p class="muted small">${[s.platform,s.conversation,s.sender,s.time].filter(Boolean).map(esc).join(' · ')}</p><blockquote class="evidence" style="white-space:pre-wrap">${esc(s.text || '无文字正文')}</blockquote>`).join('')}</details><div class="actions" id="delivery-state-actions"></div></section>`;
      renderDetailPlans();
      for (const r of task.resources || []) {
        const li = document.createElement('li'),label = document.createElement('span');label.textContent = `${r.role === 'result' ? '成果' : '材料'} · ${r.name} `;li.append(label);
        if (r.type === 'link' && /^https?:\/\//i.test(r.url || '')) {
          const a = document.createElement('a');a.textContent = '打开链接 ↗';a.href = r.url;a.target = '_blank';a.rel = 'noopener noreferrer';li.append(a);
        } else if (r.status === 'ready') {
          const b = document.createElement('button');b.className = 'text-btn';b.textContent = '下载';
          b.addEventListener('click',() => action(async () => {
            const response = await request(`/tasks/${encodeURIComponent(id)}/materials/${encodeURIComponent(r.id)}`),url = URL.createObjectURL(await response.blob()),a = document.createElement('a');
            a.href = url;a.download = r.name;a.click();setTimeout(() => URL.revokeObjectURL(url),10000);
          }));li.append(b);
        } else li.append(document.createTextNode('本地原件不可用'));
        if (r.warnings?.length) li.append(document.createTextNode(' · '+r.warnings.join('、')));
        if (r.note) {const p = document.createElement('p');p.textContent = r.note;li.append(p);}$('materials').append(li);
      }
      const pending = summary?.plans.some(p => p.state === 'feedback');
      if (pending) {const p = document.createElement('p');p.className = 'muted small';p.textContent = '该待办已有操作等待回写，请先查看上方回写状态。';$('state-actions').append(p);}
      for (const [label,type] of (pending ? [] : task.status === 'open' ? [['标记完成','complete'],['1 小时后提醒','snooze']] : ['completed','done'].includes(task.status) ? [['恢复待办','reopen']] : [])) {
        const b = document.createElement('button');b.className = 'btn';b.textContent = label;
        b.addEventListener('click',() => action(async () => {
          const body = {action:type};if (type === 'snooze') body.until = new Date(Date.now()+3600000).toISOString();
          const result = await request('/tasks/'+encodeURIComponent(id)+'/state',{method:'POST',body:JSON.stringify(body)}).then(r => r.json());
          $('task-dialog').close();await load();notice.textContent = result.status === 'queued' ? '操作已排队，等待处理层确认；当前不代表已完成回写。' : '本地状态已更新。';
          if ($('recovery').open) window.dispatchEvent(new Event('delivery-recovery-request'));
        }));$('state-actions').append(b);
      }
    } catch (error) {if ($('task-dialog').open) $('detail-body').textContent = '待办详情读取失败：'+error.message;}
  }
  async function action(callback) {
    if (busy) return;busy = true;
    try {await callback();} catch (error) {
      notice.textContent = error.message;
      if ($('task-dialog').open) {
        let message = $('detail-error');
        if (!message) {message = document.createElement('p');message.id = 'delivery-detail-error';message.setAttribute('role','alert');$('detail-body').prepend(message);}
        message.textContent = error.message;
      }
    } finally {busy = false;}
  }
  async function loadDigest() {
    const seq = ++digestSequence,params = new URLSearchParams();
    if ($('digest-date').value) params.set('date',$('digest-date').value);
    if ($('digest-target').value) params.set('target_id',$('digest-target').value);
    try {
      const result = await request('/digest?'+params).then(r => r.json());
      if (seq !== digestSequence) return;
      digest = result;$('digest-date-error').hidden = true;$('digest-preview').hidden = false;$('digest-date').value = digest.date;
      options($('digest-target'),digest.targets);$('digest-target').value = digest.target_id || '';$('digest-target').disabled = !digest.targets.length;
      const target = digest.targets.find(t => t.id === digest.target_id),stored = digest.delivery;
      const clock = new Intl.DateTimeFormat('zh-CN',{timeZone:digest.timezone,hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(digest.slot*1000));
      $('digest-clock').innerHTML = esc(clock)+' <span>'+esc(digest.timezone === 'Asia/Shanghai' ? '上海时间' : digest.timezone)+'</span>';
      $('digest-eta').textContent = stored?.status === 'sent' ? '已发送于 '+date(stored.finished_at) : stored?.status === 'unknown' ? '结果未知，请先核对，停止自动重发' : !digest.enabled ? '当前未启用，不会自动投递' : digest.expires_at <= timeNow() ? '该日补发窗口已结束' : data?.runtime.state !== 'running' ? '规则时间 '+date(digest.slot)+'；后台状态未满足，尚不能预计实际投递' : '预计 '+date(digest.slot)+' 开始 · '+remaining(digest.slot);
      $('digest-expiry').textContent = date(digest.expires_at)+' 前';
      $('digest-state').textContent = stored ? ({sent:'已发送',unknown:'结果未知',sending:'正在投递',pending:'已排队',retry:'等待重试',failed:'投递失败',obsolete:'已失效'})[stored.status] || stored.status : digest.enabled ? '早报已开启' : '早报未开启';
      $('digest-state').className = 'badge '+(digest.enabled && !stored ? 'green' : stored?.status === 'unknown' ? 'error' : '');
      $('digest-message-title').textContent = digest.date+' · 今日通知安排（'+digest.items.length+' 项）';
      $('digest-preview-title').textContent = stored?.frozen ? '当次早报正文' : '预计收到的内容';
      $('digest-recipient').textContent = target ? '投递至 '+target.name+' · 时区 '+digest.timezone : '尚未配置可接收早报的通知型目标';
      $('digest-intro').textContent = digest.items.length ? '按当前规则，当天需要通知的事项如下。' : '当天没有已知的通知安排。';
      $('digest-items').innerHTML = digest.items.map(item => `<section class="digest-item"><div class="digest-item-head">${priority(item)}<button class="text-btn" data-task-id="${esc(item.task_id)}">${esc(item.title)} <span aria-hidden="true">↗</span></button></div><div class="times">${item.notifications.map(n => `<span class="time-chip">${esc(new Intl.DateTimeFormat('zh-CN',{timeZone:digest.timezone,hour:'2-digit',minute:'2-digit',...(new Date(n.time).getUTCSeconds() ? {second:'2-digit'} : {}),hour12:false}).format(new Date(n.time)))}${n.kind === 'interval' ? ' 起按间隔' : ''}</span>`).join('')}</div><span class="sub">${item.deadline ? '截止 '+esc(item.deadline)+(item.deadline.length === 10 ? ' · 原文仅日期，按任务时区补足时刻' : '') : '无截止日期 · 按普通间隔规则'}</span></section>`).join('');
      $('digest-message-notes').textContent = stored?.frozen ? '已有投递记录，展示当次正文快照；当前待办变化不会改写这份历史内容。' : '预估基准：'+date(digest.generated_at)+'；仅汇总生成时已知安排，新增待办可能改变结果。';
      $('digest-exclusion-label').textContent = '为什么其余 '+digest.excluded.length+' 项没有出现在这封早报里？';$('digest-exclusion-label').parentElement.hidden = Boolean(stored?.frozen);
      $('digest-exclusions').innerHTML = '<ul>'+digest.excluded.map(t => `<li><button class="text-btn" data-task-id="${esc(t.task_id)}">${esc(t.title)}</button><span class="muted"> · ${esc(t.reason)}</span></li>`).join('')+'</ul>';
      $('digest-preview').querySelector('.preview-scope').textContent = stored?.frozen ? '这是消息层实际投递记录对应的内容，以目标回执为准。' : stored ? '已有本日投递记录；此处按最新已知状态重新预估，未发送正文将在后台下一轮检查时更新。' : '这是内容预估，尚未入投递队列。新增、完成、改期、延后或反馈回写都会影响最终早报；未获取的新消息不在其中。';
    } catch (error) {if (seq === digestSequence) {$('digest-preview').hidden = true;$('digest-date-error').hidden = false;$('digest-date-error').textContent = '早报预览未完成：'+error.message;}}
  }
  function switchTab(name, focus = false, refresh = true) {
    for (const key of ['tasks','digest']) {$('tab-'+key).setAttribute('aria-selected',String(name === key));$('tab-'+key).tabIndex = name === key ? 0 : -1;$(key+'-view').hidden = name !== key;}
    if (name === 'digest' && refresh) loadDigest();if (focus) $('tab-'+name).focus();
  }
  function renderRules() {
    const r = data.rules;
    $('rules-body').innerHTML = '<div class="rule-callout"><strong>有效待办接收后首次通知 + 未完成时按节点提醒</strong></div><p>原始聊天先经过采集和处理；消息层在下一轮检查时通知有效待办，不是每条原文入库即发送。内容修订也会更新。</p><h3>首次通知</h3><p>'+Object.entries(r.initial_notifications).map(([p,on]) => esc(p)+'：'+(on ? '开启' : '关闭')).join('；')+'</p><h3>截止提醒'+(r.deadline_reminders.enabled ? '' : '（已关闭）')+'</h3><table class="rules-table"><thead><tr><th>优先级</th><th>截止前提醒</th></tr></thead><tbody>'+Object.entries(r.initial_notifications).map(([p]) => '<tr><td>'+esc(p)+'</td><td>'+((r.deadline_reminders[p] || []).map(s => s ? '提前 '+duration(s) : '截止时').join('、') || '不提醒')+'</td></tr>').join('')+'</tbody></table><p class="small muted">仅日期截止按任务时区当天 23:59:59 补足。错过多个节点只检查最近一个；成功通知覆盖过的节点不重复催办。</p><div class="detail-section"><h3>无截止日期或截止提醒关闭：普通间隔</h3><p>'+Object.entries(r.reminders).map(([p,s]) => esc(p)+'：'+(s ? '每 '+duration(s) : '不周期提醒')).join('；')+'</p><p class="small muted">截止与间隔提醒不叠加；结束后停止催办，延后期间暂停通知；反馈待回写或结果未知需先处理。同步型目标只同步变更。</p></div><div class="detail-section"><h3>每日早报'+(r.daily_digest.enabled ? '' : '（已关闭）')+'</h3><p>'+esc(r.daily_digest.time)+' · '+esc(r.daily_digest.timezone)+'；补发窗口 '+duration(r.daily_digest.catch_up_minutes*60)+'</p><p class="small muted">按目标汇总当天已知通知安排，不等于全部待办或当天到期待办；同步型目标不额外接收。关机、休眠、后台停止时不会发送。</p></div>';
  }
  $('target-filter').options[0].value = '';
  const previous = document.createElement('button');previous.id = 'delivery-previous';previous.className = 'btn';previous.textContent = '上一页';previous.hidden = true;$('more').before(previous);
  previous.addEventListener('click',() => {offset = Math.max(0,offset-50);load();});$('more').textContent = '下一页';$('more').addEventListener('click',() => {offset += 50;load();});
  let debounce;
  $('task-search').addEventListener('input',() => {clearTimeout(debounce);debounce = setTimeout(() => load(true),250);});
  for (const id of ['state-filter','target-filter']) $(id).addEventListener('change',() => load(true));
  $('filter-issues').addEventListener('click',() => {$('task-search').value = '';$('target-filter').value = '';$('state-filter').value = 'attention';load(true);$('state-filter').focus();});
  $('reset-filters').addEventListener('click',() => {$('task-search').value = '';$('state-filter').value = 'all';$('target-filter').value = '';load(true);$('task-search').focus();});
  $('open-rules').addEventListener('click',() => {if (data) {renderRules();$('rules-dialog').showModal();} else load();});
  for (const key of ['tasks','digest']) {
    $('tab-'+key).addEventListener('click',() => switchTab(key));
    $('tab-'+key).addEventListener('keydown',event => {if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {event.preventDefault();switchTab(event.key === 'Home' ? 'tasks' : event.key === 'End' ? 'digest' : key === 'tasks' ? 'digest' : 'tasks',true);}});
  }
  for (const id of ['digest-date','digest-target']) $(id).addEventListener('change',loadDigest);
  $('detail-to-digest').addEventListener('click',() => {$('task-dialog').close();switchTab('digest',true);});
  document.getElementById('view-delivery').addEventListener('click',event => {
    const task = event.target.closest('[data-task-id]');if (task) showTask(task.dataset.taskId);
    if (event.target.closest('[data-delivery-close]')) event.target.closest('dialog').close();
    if (event.target.closest('[data-delivery-recovery]')) {$('task-dialog').close();$('recovery').open = true;$('recovery').scrollIntoView({block:'start'});}
  });
  $('task-dialog').addEventListener('close',() => {
    const id = detailTask?.task_id;detailTask = null;
    if (id && (document.activeElement === document.body || $('task-dialog').contains(document.activeElement))) {
      const pane = $('digest-view').hidden ? $('tasks-view') : $('digest-view');
      (pane.querySelector(`[data-task-id="${CSS.escape(id)}"]`) || $('tab-'+($('digest-view').hidden ? 'tasks' : 'digest'))).focus({preventScroll:true});
    }
  });
  $('recovery').addEventListener('toggle',() => {if ($('recovery').open) window.dispatchEvent(new Event('delivery-recovery-request'));});
  document.getElementById('messaging-preview').addEventListener('click',() => load());
  document.getElementById('messaging-sync').addEventListener('click',() => action(async () => {await request('/sync',{method:'POST'});await load();notice.textContent = '已导入并保存本地材料，未由此操作发送消息。';}));
  function enter() {
    if (document.hidden || document.getElementById('view-delivery').hidden) return;
    if (location.hash === '#delivery/digest') switchTab('digest',false,false);load();
  }
  window.addEventListener('hashchange',() => {for (const id of ['task-dialog','rules-dialog']) if ($(id).open) $(id).close();enter();});
  document.addEventListener('visibilitychange',enter);
  window.addEventListener('operations-updated',() => {if (!document.hidden && !document.getElementById('view-delivery').hidden) load();});
  setInterval(() => {if (!loading && !document.hidden && !document.getElementById('view-delivery').hidden) load();},15000);
  setInterval(() => {if (!document.hidden && !document.getElementById('view-delivery').hidden) for (const el of document.querySelectorAll('#view-delivery [data-delivery-time]')) el.textContent = remaining(Number(el.dataset.deliveryTime));},1000);
  enter();
});
