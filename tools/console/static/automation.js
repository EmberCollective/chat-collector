'use strict';
document.addEventListener('DOMContentLoaded', () => {
  const node = (tag, text, cls) => {const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el;};
  const byId = id => document.getElementById('ops-' + id);
  const jobNames = {processor: '自动处理', messaging: '自动投递', feedback: '状态回写', listener: '飞书按钮监听'};
  const kindNames = {feishu_app: '飞书应用（交互卡片）', feishu: '飞书群机器人', notion: 'Notion / Notion Calendar', webhook: '通用 Webhook'};
  const labels = {enabled: '启用', interval_seconds: '间隔（秒）', max_attempts: '失败尝试上限', backoff_seconds: '首次重试等待（秒）', max_backoff_seconds: '最长重试等待（秒）', retry_base_seconds: '重试等待基数（秒）', max_deliveries_per_round: '每轮最多投递数', timeout_seconds: '请求超时（秒）', app_id_env: '应用 ID 环境变量名', app_secret_env: '应用密钥环境变量名', recipient_id: '目标群 chat_id', receive_id_type: '目标类型', allowed_operator_ids: '可操作人员 open_id（每行一个）', snooze_seconds: '稍后提醒等待（秒）', upload_files: '上传本地附件', max_file_bytes: '附件上传上限（字节）', url_env: 'Webhook 地址环境变量名', secret_env: '签名密钥环境变量名（可空）', token_env: '访问令牌环境变量名', data_source_id: 'Notion 数据源 ID', mode: '投递模式', purpose: '用途说明', title: '标题', task_id: '待办编号', priority: '优先级', status: '状态', date: '日期', instructions: '行动说明', files: '附件'};
  let config = null, dirty = false, busy = false, polling = false;
  let controls = [];
  const notice = text => {byId('notice').textContent = text; byId('delivery-notice').textContent = text;};
  const markDirty = () => {dirty = true; notice('有未保存的设置。运行控制与测试使用已保存的配置。');};
  async function request(path, options = {}) {
    const session = await fetch('/api/session').then(r => r.json());
    const response = await fetch('/api/operations' + path, {...options, headers: {'X-Console-Token': session.token, 'Content-Type': 'application/json'}});
    const body = await response.json();
    if (!response.ok) throw Error(response.status === 409 && path === '/config' ? '配置已变化或正在操作。你的编辑已保留；请先核对后重新加载。' : typeof body.detail === 'string' ? body.detail : '操作失败，请检查输入和运行状态。');
    return body;
  }
  async function action(callback) {
    if (busy) return;
    busy = true;
    document.querySelectorAll('[data-ops-action]').forEach(b => b.disabled = true);
    try {await callback();} catch (error) {notice(error.message);} finally {
      busy = false;
      document.querySelectorAll('[data-ops-action]').forEach(b => b.disabled = false);
    }
  }
  function button(parent, text, callback) {
    const el = node('button', text, 'secondary'); el.type = 'button'; el.dataset.opsAction = 'true';
    el.addEventListener('click', () => action(callback)); parent.append(el); return el;
  }
  function details(parent, text) {const el = node('details'); el.append(node('summary', text)); parent.append(el); return el;}
  function grid(parent) {const el = node('div', null, 'settings-grid'); parent.append(el); return el;}
  function field(parent, object, key, type = 'string', options = {}) {
    const label = node('label', options.label || labels[key] || key);
    const input = node(type === 'strings' ? 'textarea' : options.choices ? 'select' : 'input');
    if (options.choices) for (const [value, title] of Object.entries(options.choices)) {const option = node('option', title); option.value = value; input.append(option);}
    else if (type !== 'strings') input.type = type === 'boolean' ? 'checkbox' : type === 'integer' ? 'number' : 'text';
    input.id = 'ops-field-' + controls.length;
    if (type === 'boolean') input.checked = !!object[key];
    else input.value = type === 'strings' ? (object[key] || []).join('\n') : object[key] ?? '';
    if (type === 'integer') {input.min = options.min ?? 1; input.max = options.max ?? 86400; input.step = '1'; input.required = true;}
    if (type === 'env') {input.pattern = '[A-Za-z_][A-Za-z0-9_]{0,127}'; input.placeholder = '仅填写变量名；密钥值保留在本机 .env'; input.maxLength = 128;}
    if (type === 'string') input.maxLength = 500;
    if (type === 'strings') input.rows = 3;
    input.addEventListener('input', markDirty); input.addEventListener('change', markDirty);
    label.append(input); parent.append(label);
    controls.push({input, object, key, type, optional: options.optional});
    return input;
  }
  function jobFields(parent, name) {
    const settings = config.automation.jobs[name];
    parent.append(node('h4', jobNames[name])); const main = grid(parent);
    field(main, settings, 'enabled', 'boolean', {label: '定时运行' + jobNames[name]}); field(main, settings, 'interval_seconds', 'integer');
    const advanced = grid(details(parent, '失败重试设置'));
    for (const key of ['max_attempts', 'backoff_seconds', 'max_backoff_seconds']) field(advanced, settings, key, 'integer', {max: key === 'max_attempts' ? 20 : 86400});
    const actions = node('div', null, 'actions'); parent.append(actions);
    button(actions, '运行一轮', () => operate('/jobs/' + name + '/once', jobNames[name] + '已请求运行。'));
    button(actions, '恢复重试', () => operate('/jobs/' + name + '/resume', jobNames[name] + '已恢复。'));
  }
  function targetCard(target) {
    if (target.kind === 'feishu_app') {target.max_file_bytes ??= 30000000; target.snooze_seconds ??= 3600; target.receive_id_type ??= 'chat_id'; target.upload_files ??= true;}
    if (target.kind === 'webhook') target.mode ??= 'notify';
    const card = node('article', null, 'message'); byId('targets').append(card);
    card.append(node('h4', `${target.id} · ${kindNames[target.kind] || target.kind}`));
    const schema = config.messaging.schemas[target.kind] || {};
    const fields = grid(card);
    for (const [key, type] of Object.entries(schema)) {
      if (['id', 'kind', 'properties'].includes(key)) continue;
      const options = {optional: type === 'env'};
      if (key === 'timeout_seconds') options.max = 300;
      if (key === 'snooze_seconds') options.max = 31536000;
      if (key === 'max_file_bytes') options.max = 30000000;
      if (key === 'receive_id_type') options.choices = {chat_id: '群聊 chat_id'};
      if (key === 'mode') options.choices = {notify: '消息通知', sync: '同步更新'};
      field(fields, target, key, type, options);
    }
    if (target.kind === 'notion') {
      card.append(node('p', '同步到 Notion 数据源；在 Notion Calendar 中连接该数据源的日期字段。', 'muted'));
      target.properties ||= {};
      const props = grid(details(card, 'Notion 属性名映射（留空沿用现有或默认值）'));
      for (const key of ['title', 'task_id', 'priority', 'status', 'date', 'instructions', 'files']) field(props, target.properties, key, 'string', {optional: true});
    }
    const actions = node('div', null, 'actions'); card.append(actions);
    button(actions, '发送测试消息…', async () => {
      if (dirty) throw Error('请先保存设置，再测试已保存的通道。');
      if (!window.confirm(`向 ${target.id} 发送一条测试消息？这会访问外部服务。`)) return;
      const result = await request('/targets/' + encodeURIComponent(target.id) + '/test', {method: 'POST'});
      notice(result.status === 'failed' ? '测试失败，请查看通道测试记录。' : result.status === 'unknown' ? '测试是否送达尚不确定，请先核对目标位置，避免重复发送。' : '测试已发送，请核对目标位置与通道测试记录。'); await refreshStatus();
    });
  }
  function renderConfig() {
    controls = [];
    for (const id of ['processor-fields', 'delivery-fields', 'targets']) byId(id).replaceChildren();
    const processor = byId('processor-fields'), delivery = byId('delivery-fields');
    field(grid(processor), config.automation, 'enabled', 'boolean', {label: '启用自动运行服务'});
    jobFields(processor, 'processor');
    const globals = grid(delivery);
    field(globals, config.messaging, 'enabled', 'boolean', {label: '允许外部投递'});
    field(globals, config.messaging, 'feedback_enabled', 'boolean', {label: '允许操作结果回写处理层'});
    jobFields(delivery, 'messaging'); jobFields(delivery, 'feedback');
    field(grid(delivery), config.automation.listener, 'enabled', 'boolean', {label: '启用飞书按钮监听（长连接）'});
    delivery.append(node('p', '保存监听开关后，启动或重新启动后台服务生效。', 'muted'));
    const listenerActions = node('div', null, 'actions'); delivery.append(listenerActions);
    button(listenerActions, '恢复按钮监听重试', () => operate('/jobs/listener/resume', '已恢复按钮监听。'));
    const reminders = grid(details(delivery, '优先级提醒间隔（秒；0 表示不重复提醒）'));
    for (const key of ['P0', 'P1', 'P2', 'P3', 'P4']) field(reminders, config.messaging.reminders, key, 'integer', {min: 0, max: 31536000});
    const retry = details(delivery, '投递与状态回写的重试上限');
    retry.append(node('p', '自动运行间隔控制上方服务；以下间隔也供独立运行消息层时使用。', 'muted'));
    for (const section of ['schedule', 'feedback']) {
      retry.append(node('h4', section === 'schedule' ? '消息投递' : '操作回写'));
      const area = grid(retry);
      for (const key of Object.keys(config.messaging[section])) field(area, config.messaging[section], key, 'integer', {max: key === 'max_attempts' ? 20 : key === 'max_deliveries_per_round' ? 1000 : 86400});
    }
    const alerts = grid(details(delivery, '运行失败通知'));
    field(alerts, config.automation.alerts, 'enabled', 'boolean', {label: '启用运行失败通知'});
    field(alerts, config.automation.alerts, 'target_id', 'string', {label: '接收通知的通道编号'});
    field(alerts, config.automation.alerts, 'cooldown_seconds', 'integer', {label: '重复故障通知间隔（秒）', max: 2592000});
    field(alerts, config.automation.alerts, 'recovery_enabled', 'boolean', {label: '恢复后发送通知'});
    config.messaging.targets.forEach(targetCard);
    byId('new-kind').replaceChildren();
    for (const kind of Object.keys(config.messaging.schemas)) {const option = node('option', kindNames[kind] || kind); option.value = kind; byId('new-kind').append(option);}
  }
  function collect() {
    for (const {input} of controls) if (!input.checkValidity()) {
      let parent = input.parentElement; while (parent) {if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement;}
      input.reportValidity(); input.focus(); throw Error('请修正标出的设置后保存。');
    }
    for (const {input, object, key, type, optional} of controls) {
      if (optional && !input.value.trim()) {delete object[key]; continue;}
      object[key] = type === 'boolean' ? input.checked : type === 'integer' ? Number(input.value) : type === 'strings' ? input.value.split(/[\n,，]+/).map(x => x.trim()).filter(Boolean) : input.value.trim();
    }
    const {schemas, ...messaging} = config.messaging;
    return {version: config.version, automation: config.automation, messaging};
  }
  async function loadConfig() {
    if (dirty && !window.confirm('重新加载会放弃未保存的自动运行和通道设置。继续？')) return;
    config = await request('/config'); renderConfig(); dirty = false; notice('设置已加载。保存只更新配置；服务未运行时需点击启动。');
  }
  async function saveConfig() {
    if (!config) throw Error('请先重新加载设置。');
    const result = await request('/config', {method: 'PUT', body: JSON.stringify(collect())});
    config = {version: result.version, automation: result.automation, messaging: result.messaging};
    renderConfig(); dirty = false;
    notice(result.operation ? '已保存，运行中的服务正在按新配置重新启动或停止。' : '已保存。服务尚未运行，可点击启动后台服务。');
    await refreshStatus();
  }
  async function operate(path, message) {
    if (dirty) throw Error('请先保存设置或重新加载，再执行运行操作。');
    const result = await request(path, {method: 'POST'});
    notice(result.state && states[result.state] ? states[result.state] : message); await refreshStatus();
  }
  const states = {idle: '等待', running: '运行中', retry: '等待重试', paused: '已暂停', starting: '启动中', stopping: '停止中（等待当前任务完成）', stopped: '已停止', disabled: '未启用', failed: '失败', pending: '等待执行', unknown: '状态不明，请核实', delivered: '已送达', conflict: '状态冲突', applied: '已回写', superseded: '已替代', sent: '已发送', queued: '已排队', waiting: '等待当前任务完成', busy: '已有操作正在进行', duplicate: '发现重复后台进程，请检查', restarting: '正在重新启动', not_running: '未运行', config_changed: '配置已变化，请重新启动', configuration_error: '配置未就绪，请检查运行状态', setup_listener_active: '已有连接检查程序正在监听，请先停止旧连接检查', test_received: '按钮回调已收到'};
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '—';
  function table(parent, headers, rows) {
    if (!rows.length) {parent.append(node('p', '暂无记录。', 'muted')); return;}
    const wrap = node('div', null, 'table-wrap'), table = node('table'), head = node('tr'), body = node('tbody');
    headers.forEach(text => head.append(node('th', text))); const thead = node('thead'); thead.append(head); table.append(thead, body);
    for (const cells of rows) {const row = node('tr'); for (const cell of cells) {const td = node('td'); if (cell instanceof Node) td.append(cell); else td.textContent = cell ?? '—'; row.append(td);} body.append(row);}
    wrap.append(table); parent.append(wrap);
  }
  function countdowns() {
    document.querySelectorAll('[data-ops-next]').forEach(el => {const remaining = Math.ceil(Number(el.dataset.opsNext) - Date.now() / 1000); el.textContent = remaining > 0 ? `${remaining} 秒后` : '等待调度';});
  }
  function renderStatus(snapshot) {
    const service = snapshot.service || {}, active = service.running;
    byId('service-status').textContent = `后台服务：${active ? states[service.state] || '运行中' : states[service.state] || service.state || '未运行'}${service.pid ? ' · PID ' + service.pid : ''}${service.config_changed ? ' · 配置已变化，请重新启动' : ''}`;
    const jobs = byId('jobs'); jobs.replaceChildren();
    const checks = snapshot.checks || {};
    const warnings = {'Disable matching console schedules before enabling automation collectors.': '启用后台采集时，请关闭采集台对应的定时任务，避免重复调度。', 'Configuration changes require restarting automation; check performs no live API validation.': '配置变更需重新启动后台服务；配置检查不验证外部服务连接。'};
    if (checks.warnings?.length) jobs.append(node('p', checks.warnings.map(text => warnings[text] || text).join(' '), 'scope'));
    const entries = [...(snapshot.automation?.jobs || [])];
    if (snapshot.automation?.listener) entries.push({...snapshot.automation.listener, name: 'listener', enabled: checks.jobs?.listener?.enabled});
    table(jobs, ['任务', '状态', '失败尝试次数', '下次运行', '上次成功', '最近故障 / 配置检查'], entries.map(job => {
      const next = node('span', '—');
      if (active && job.enabled && job.next_run && job.state !== 'paused') next.dataset.opsNext = job.next_run;
      const check = checks.jobs?.[job.name];
      return [jobNames[job.name] || job.name, !job.enabled ? '未启用' : !active ? '服务未运行' : states[job.state] || job.state, job.attempts ?? 0, next, date(job.last_success), job.active_error || job.error || (check?.ready === false ? check.reason : '') || '—'];
    }));
    const recovery = byId('recovery'); recovery.replaceChildren();
    recovery.append(node('h4', '投递失败与不确定结果'));
    const deliveries = (snapshot.messaging?.deliveries || []).filter(item => ['failed', 'unknown', 'retry'].includes(item.status));
    table(recovery, ['待办 / 通道', '状态', '故障', '恢复'], deliveries.map(item => {
      const actions = node('div', null, 'actions');
      if (['failed', 'unknown'].includes(item.status)) button(actions, item.status === 'unknown' ? '已核实未送达，重试' : '重新投递', async () => {
        if (!window.confirm('确认重试这条投递？状态不明时，请先在目标端核实未收到，避免重复。')) return;
        await operate('/deliveries/' + encodeURIComponent(item.id) + '/retry', '已重新排入投递队列。');
      });
      if (item.status === 'unknown') button(actions, '已核实送达', async () => {
        if (!window.confirm('确认已在目标端核实收到这条消息？')) return;
        await operate('/deliveries/' + encodeURIComponent(item.id) + '/delivered', '已记录送达。');
      });
      return [(item.title || '待办投递 · ' + String(item.task_id).slice(-8)) + ' / ' + item.target_id, states[item.status], item.error, actions];
    }));
    recovery.append(node('h4', '完成、取消和稍后提醒的回写'));
    const requests = (snapshot.feedback?.requests || []).filter(item => !['applied', 'superseded'].includes(item.status));
    table(recovery, ['待办', '状态', '故障', '恢复'], requests.map(item => {
      const actions = node('div');
      if (['failed', 'retry'].includes(item.status)) button(actions, '重试回写', () => operate('/feedback/' + encodeURIComponent(item.request_id) + '/retry', '已安排回写重试。'));
      if (item.status === 'conflict') actions.append(node('span', '请刷新待办后重新操作；旧请求不能覆盖较新的修改。'));
      return [item.title || '待办操作 · ' + String(item.task_id).slice(-8), states[item.status] || item.status, item.error, actions];
    }));
    const tests = byId('tests'); tests.replaceChildren();
    const records = Array.isArray(snapshot.tests) ? snapshot.tests : snapshot.tests?.records || snapshot.tests?.tests || [];
    table(tests, ['时间', '通道', '结果', '按钮回调'], records.map(item => [date(item.created_at || item.time), item.target_id, (states[item.status] || item.status) + (item.error ? ' · ' + item.error : ''), item.callback?.status === 'test_received' ? '已收到 · ' + date(item.callback.clicked_at) : '—']));
    countdowns();
  }
  async function refreshStatus() {
    if (polling) return;
    polling = true;
    try {renderStatus(await request('/status')); byId('status-error').textContent = '';}
    catch (error) {byId('status-error').textContent = '状态刷新失败：' + error.message + '（稍后自动重试，未保存设置不受影响）';}
    finally {polling = false;}
  }
  for (const id of ['save', 'delivery-save']) byId(id).addEventListener('click', () => action(saveConfig));
  for (const id of ['reload', 'delivery-reload']) byId(id).addEventListener('click', () => action(loadConfig));
  for (const command of ['start', 'stop', 'restart']) byId(command).addEventListener('click', () => action(() => operate('/service/' + command, '后台服务操作已提交。')));
  byId('refresh').addEventListener('click', refreshStatus);
  byId('add-target').addEventListener('click', () => action(async () => {
    if (!config) throw Error('请先加载配置。');
    const id = byId('new-id').value.trim(), kind = byId('new-kind').value;
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(id)) throw Error('通道编号只能包含字母、数字、下划线或连字符，最多 100 字符。');
    if (config.messaging.targets.some(target => target.id === id)) throw Error('通道编号已存在。');
    const target = {id, kind, enabled: false, timeout_seconds: 30};
    if (kind === 'feishu_app') Object.assign(target, {receive_id_type: 'chat_id', snooze_seconds: 3600, max_file_bytes: 30000000, upload_files: true});
    if (kind === 'webhook') target.mode = 'notify';
    config.messaging.targets.push(target); targetCard(target); byId('new-id').value = ''; markDirty();
  }));
  window.addEventListener('beforeunload', event => {if (dirty) {event.preventDefault(); event.returnValue = '';}});
  action(loadConfig); refreshStatus();
  setInterval(refreshStatus, 5000); setInterval(countdowns, 1000);
});
