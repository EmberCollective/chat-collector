'use strict';
document.addEventListener('DOMContentLoaded', () => {
  const node = (tag, text, cls) => {const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el;};
  const byId = id => document.getElementById('ops-' + id);
  const jobNames = {listener: '渠道按钮监听'};
  let jobCatalog = {}, sourceDescriptions = [];
  const expandedTargets = new Set();
  const labels = {enabled: '启用', interval_seconds: '间隔（秒）', max_attempts: '失败尝试上限', backoff_seconds: '首次重试等待（秒）', max_backoff_seconds: '最长重试等待（秒）', retry_base_seconds: '重试等待基数（秒）', max_deliveries_per_round: '每轮最多投递数', timeout_seconds: '请求超时（秒）', app_id_env: '应用 ID 环境变量名', app_secret_env: '应用密钥环境变量名', recipient_id: '目标群 chat_id', receive_id_type: '目标类型', allowed_operator_ids: '可操作人员 open_id（每行一个）', snooze_seconds: '稍后提醒等待（秒）', upload_files: '上传本地附件', max_file_bytes: '附件上传上限（字节）', url_env: 'Webhook 地址环境变量名', secret_env: '签名密钥环境变量名（可空）', token_env: '访问令牌环境变量名', data_source_id: 'Notion 数据源 ID', mode: '投递模式', purpose: '用途说明', title: '标题', task_id: '待办编号', priority: '优先级', status: '状态', date: '日期', instructions: '行动说明', files: '附件'};
  let config = null, dirty = false, busy = false, polling = false;
  let controls = [];
  const notice = (text, service = true) => {byId('notice').textContent = text; byId('action-notice').textContent = service ? text : '';if(byId('target-dialog').open)byId('add-error').textContent=text;};
  const markDirty = () => {dirty = true; notice('有未保存的设置。运行控制与测试使用已保存的配置。', false);};
  async function request(path, options = {}, base = '/api/operations') {
    const session = await window.consoleSession();
    const response = await fetch(base + path, {...options, headers: {'X-Console-Token': session.token, 'Content-Type': 'application/json'}});
    const body = await response.json();
    if (!response.ok) throw Error(response.status === 409 && path === '/config' ? '配置已变化或正在操作。你的编辑已保留；请先核对后重新加载。' : typeof body.detail === 'string' ? body.detail : '操作失败，请检查输入和运行状态。');
    return body;
  }
  async function action(callback) {
    if (busy) return;
    let payload;
    try {if(callback===saveConfig)payload=collect();}catch(error){notice(error.message);return;}
    busy = true;
    controls.forEach(({input})=>input.disabled=true);
    document.querySelectorAll('[data-ops-action]').forEach(b => b.disabled = true);
    try {await callback(payload);} catch (error) {notice(error.message);} finally {
      busy = false;
      controls.forEach(({input})=>input.disabled=false);
      document.querySelectorAll('[data-ops-action]').forEach(b => b.disabled = b.dataset.opsUnavailable === 'true');
    }
  }
  function button(parent, text, callback) {
    const el = node('button', text, 'btn'); el.type = 'button'; el.dataset.opsAction = 'true';el.disabled=busy;
    el.addEventListener('click', () => action(callback)); parent.append(el); return el;
  }
  function details(parent, text) {const el = node('details'); el.append(node('summary', text)); parent.append(el); return el;}
  function grid(parent) {const el = node('div', null, 'settings-grid'); parent.append(el); return el;}
  function field(parent, object, key, type = 'string', options = {}) {
    const title=options.label||labels[key]||key;
    const label = node('label', type==='boolean'?null:title, type === 'boolean' ? 'row' : 'field');
    const input = node(type === 'strings' ? 'textarea' : options.choices ? 'select' : 'input');
    if (options.choices) for (const [value, title] of Object.entries(options.choices)) {const option = node('option', title); option.value = value; input.append(option);}
    else if (type !== 'strings') input.type = type === 'boolean' ? 'checkbox' : type === 'integer' ? 'number' : 'text';
    input.id = 'ops-field-' + controls.length;input.dataset.opsKey = key;
    input.disabled=busy;
    if (type === 'boolean') input.checked = !!object[key];
    else if (type === 'boolean-select') input.value = String(!!object[key]);
    else input.value = type === 'minutes' ? (object[key] || []).map(v => v / 60).join(', ') : type === 'strings' ? (object[key] || []).join('\n') : object[key] ?? '';
    if (type === 'integer') {input.min = options.min ?? 1; input.max = options.max ?? 86400; input.step = '1'; input.required = true;}
    if (type === 'env') {input.pattern = '[A-Za-z_][A-Za-z0-9_]{0,127}'; input.placeholder = '仅填写变量名；密钥值保留在本机 .env'; input.maxLength = 128;}
    if (type === 'string') input.maxLength = 500;
    if (type === 'strings') input.rows = 3;
    input.addEventListener('input', markDirty); input.addEventListener('change', markDirty);
    label.append(input);if(type==='boolean')label.append(node('span',title));parent.append(label);
    controls.push({input, object, key, type, optional: options.optional});
    return input;
  }
  function jobExtra(parent, name) {
    const settings=config.automation.jobs[name], section=node('section',null,'automation-job-extra');section.dataset.job=name;parent.append(section);
    section.append(node('h3',jobNames[name]||name),node('p',jobCatalog[name]?.config_file||'配置路径未提供','muted path'));
    const advanced=grid(section);
    for(const key of ['max_attempts','backoff_seconds','max_backoff_seconds'])field(advanced,settings,key,'integer',{max:key==='max_attempts'?20:86400});
    const actions=node('div',null,'row wrap');section.append(actions);
    button(actions,'运行一轮',()=>operate('/jobs/'+encodeURIComponent(name)+'/once','已请求运行一轮；结果以运行记录为准。'));
    button(actions,'恢复重试',()=>operate('/jobs/'+encodeURIComponent(name)+'/resume','已提交恢复重试。'));
  }
  function jobRow(parent,name,note) {
    const settings=config.automation.jobs[name],row=node('div',null,'automation-task-row'),main=node('div');row.dataset.job=name;parent.append(row);row.append(main);
    field(main,settings,'enabled','boolean',{label:jobCatalog[name]?.action==='process'?'处理正式增量':jobNames[name]||name});if(note)main.append(node('p',note));
    const number=node('div',null,'automation-number');row.append(number);const input=field(number,settings,'interval_seconds','integer',{label:'检查间隔'});input.parentElement.className='automation-number';number.append(node('span','秒'));
  }
  async function openRules(id){
    if(!sourceInfo(id)?.capabilities.sql)throw Error('此来源未提供 SQL 规则编辑能力。');
    if(selected()!==id||document.getElementById('module').dataset.loaded!==id){document.getElementById('module').value=id;await switchModule();}
    location.hash='automation/rules';showView();document.getElementById('automation-rules').scrollIntoView({block:'nearest'});
  }
  function sourceRow(source,entry){
    const [name,description]=entry||[],settings=name?config.automation.jobs[name]:null;
    const row=node('div',null,'automation-source-row '+(settings?'scheduled':'external')),identity=node('div');row.dataset.sourceId=source.id;row.append(identity);identity.append(node('strong',source.name),node('small',source.id));
    row.append(node('span',settings?'本机后台':source.capabilities?.schedule?'未关联后台计划':'外部自行管理','automation-mode'));
    if(settings){
      const toggle=field(row,settings,'enabled','boolean',{label:'启用'});toggle.parentElement.className='source-switch';toggle.setAttribute('aria-label','启用 '+source.name+' 后台采集');
      const number=node('label',null,'automation-number');number.append(node('span','每','automation-mobile-label'));const input=field(number,settings,'interval_seconds','integer',{label:'检查间隔'});input.setAttribute('aria-label',source.name+'检查间隔，秒');input.parentElement.replaceWith(input);number.append(node('span','秒'));row.append(number);
    }else row.append(node('span','—','automation-unavailable'),node('span','—','automation-unavailable'));
    if(source.capabilities?.sql){const link=button(row,'SQL 规则与参数 →',()=>openRules(source.id));link.className='text-btn';}
    else row.append(node('span','由来源管理','automation-mode'));
    byId('source-fields').append(row);
  }
  function channelDescription(kind) {const types=config?.messaging.types||{};return Object.hasOwn(types,kind)?types[kind]:null;}
  function targetCard(target) {
    const description=channelDescription(target.kind);
    const card=node('details',null,'channel');card.dataset.targetId=target.id;card.open=expandedTargets.has(target.id);byId('targets').append(card);
    card.addEventListener('toggle',()=>{if(card.open)expandedTargets.add(target.id);else expandedTargets.delete(target.id);});
    const summary=node('summary',null,'automation-target-summary'),title=node('span');title.append(node('strong',target.id),node('small',description?.name||target.kind));summary.append(title,node('span',target.enabled?'已启用':'未启用','chip'+(target.enabled?' green':'')),node('span',description?Object.entries(description.capabilities||{}).filter(([,enabled])=>enabled).map(([name])=>({send:'消息',files:'文件',feedback:'回写',listen:'监听',listener:'监听',test:'测试',recovery:'结果核实'}[name]||name)).join(' · '):'描述不可用','automation-caption'));card.append(summary);
    const body=node('div',null,'automation-target-body');card.append(body);card.addEventListener('change',event=>{if(event.target.dataset.opsKey==='enabled'){const chip=summary.querySelector('.chip');chip.textContent=event.target.checked?'已启用 · 草稿':'未启用 · 草稿';chip.classList.toggle('green',event.target.checked);}});
    if(!description){body.append(node('p','此类型没有可用描述，当前只读。已有配置保留在文件中，不会在保存其他设置时丢弃。','scope'));return;}
    for(const [key,value]of Object.entries(description.defaults||{}))if(target[key]===undefined)target[key]=structuredClone(value);
    body.append(node('p',description.description,'muted'));
    const fields=grid(body);
    for(const [key,definition]of Object.entries(description.fields||{})){
      if(['id','kind'].includes(key))continue;
      const type=definition.type,options={...definition,optional:type==='env'};
      if(definition.fields){
        target[key]??={};const area=grid(details(body,definition.label||key));
        for(const [child,metadata]of Object.entries(definition.fields))field(area,target[key],child,metadata.type||'string',{...metadata,optional:true});
      }else if(['string','strings','integer','boolean','env'].includes(type))field(fields,target,key,type,options);
      else{body.append(node('p',(definition.label||key)+'：当前字段只读','scope'),node('pre',JSON.stringify(target[key]??null,null,2)));}
    }
    if(!description.capabilities?.test)return;
    const actions=node('div',null,'actions');body.append(actions);
    button(actions,'发送测试消息…',async()=>{
      if(!channelDescription(target.kind)?.capabilities?.test)throw Error('此通道不支持测试');
      if(dirty)throw Error('请先保存设置，再测试已保存的通道。');
      if(!window.confirm(`向 ${target.id} 发送一条测试消息？这会访问外部服务。`))return;
      const result=await request('/targets/'+encodeURIComponent(target.id)+'/test',{method:'POST'});
      notice(result.status==='failed'?'测试失败，请查看通道测试记录。':result.status==='unknown'?'测试是否送达尚不确定，请先核对目标位置，避免重复发送。':'测试已发送，请核对目标位置与通道测试记录。');await refreshStatus();
    });
  }
  function renderConfig() {
    controls=[];
    for(const id of ['source-fields','source-advanced','processor-fields','processor-advanced','delivery-fields','delivery-advanced','reminder-fields','targets'])byId(id).replaceChildren();
    const entries=Object.entries(jobCatalog),collectors=entries.filter(([,description])=>description.action==='collect');
    for(const source of sourceDescriptions)sourceRow(source,collectors.find(([,description])=>description.source_id===source.id));
    for(const [name,description]of collectors){if(!sourceDescriptions.some(source=>source.id===description.source_id))sourceRow({id:name,name:jobNames[name]||name,capabilities:{}},[name,description]);jobExtra(byId('source-advanced'),name);}
    field(grid(byId('source-advanced')),config.automation,'enabled','boolean',{label:'允许后台按计划自动运行'});
    const processor=byId('processor-fields'),delivery=byId('delivery-fields'),reminders=byId('reminder-fields');
    const additional=entries.filter(([,description])=>description.action!=='collect');
    for(const [name,description]of additional.filter(([,description])=>!['deliver','feedback'].includes(description.action))){jobRow(processor,name,'每轮上下文、工具调用与结果可在 Pi 工作流查看');}
    for(const [name]of additional.filter(([,description])=>description.action==='deliver'))jobRow(delivery,name,'按已保存配置投递正式待办');
    const listener=details(delivery,'监听与操作回写');listener.className='automation-advanced';listener.open=true;
    for(const [name]of additional.filter(([,description])=>description.action==='feedback'))jobRow(listener,name,'完成、取消和稍后提醒等待处理层确认保存');
    const listenerRow=node('div',null,'automation-task-row'),listenerMain=node('div');listener.append(listenerRow);listenerRow.append(listenerMain);field(listenerMain,config.automation.listener,'enabled','boolean',{label:'监听渠道操作'});listenerMain.append(node('p','仅对声明监听能力的渠道生效'));
    const listenerInterval=node('div',null,'automation-number');listenerRow.append(listenerInterval);const listenerCheck=field(listenerInterval,config.automation.listener,'check_interval_seconds','integer',{label:'存活检查',max:60});listenerCheck.parentElement.className='automation-number';listenerInterval.append(node('span','秒'));
    for(const parent of [processor,delivery]){
      const advanced=details(byId(parent===processor?'processor-advanced':'delivery-advanced'),'运行一轮与失败重试设置');
      for(const [name,description]of additional.filter(([,description])=>(['deliver','feedback'].includes(description.action)?delivery:processor)===parent))jobExtra(advanced,name);
      if(parent===delivery){button(advanced,'恢复监听重试',()=>operate('/jobs/listener/resume','已提交恢复监听重试。'));advanced.append(node('p','同一应用的目标可共用监听；不同应用需分别启动监听程序。监听仅用于渠道声明支持的操作。','automation-boundary'));}
    }
    const limits=grid(details(byId('delivery-advanced'),'投递与回写限制'));
    field(limits,config.messaging,'enabled','boolean',{label:'允许外部投递'});field(limits,config.messaging,'feedback_enabled','boolean',{label:'允许操作结果回写处理层'});
    let reminderAdvanced;
    if(config.messaging.deadline_reminders){
      const settings=config.messaging.deadline_reminders;
      const checkRow=node('div',null,'automation-task-row'),checkMain=node('div'),schedule=additional.find(([,description])=>description.action==='deliver');
      checkMain.append(node('strong','检查到期提醒'),node('p','与消息投递共用计划，无需单独启动'));checkRow.append(checkMain,node('span',schedule?'每 '+config.automation.jobs[schedule[0]].interval_seconds+' 秒 · 已保存计划':'计划未关联','automation-caption'));reminders.append(checkRow);
      reminders.append(node('p','按截止节点提醒；无截止时间时使用间隔。已完成、取消的事项不再催办'));
      const mainFields=grid(reminders);
      field(mainFields,settings,'enabled','boolean-select',{label:'提醒方式',choices:{true:'截止节点优先，无截止时间用间隔',false:'仅使用间隔提醒'}});
      const p1=field(mainFields,settings,'P1','minutes',{label:'P1 截止前提醒 · 分钟'});p1.parentElement.append(node('span','0 表示截止时提醒','hint'));
      const more=details(reminders,'其他优先级、重复提醒与投递时间'),fields=grid(more);
      reminderAdvanced=more;
      field(fields,settings,'timezone','string',{label:'日期未注明时区时使用'});
      for(const key of ['P0','P2','P3','P4'])field(fields,settings,key,'minutes',{label:key+' · 提前分钟（0 为截止时）'});
      more.append(node('p','每个节点只提醒一次；错过多个节点只补最近一次。只有日期时按当日 23:59:59 安排，空列表表示该优先级不做截止提醒。','automation-boundary'));
    }
    reminderAdvanced??=details(reminders,'优先级、重复提醒与投递时间');
    if(config.messaging.initial_notifications){const fields=grid(details(reminderAdvanced,'新待办首次通知'));for(const key of ['P0','P1','P2','P3','P4'])field(fields,config.messaging.initial_notifications,key,'boolean',{label:key+' · 首次立即通知'});}
    if(config.messaging.daily_digest){
      const section=details(reminderAdvanced,'每日早报 · 今日通知安排'),fields=grid(section),settings=config.messaging.daily_digest;
      section.append(node('p','只汇总今天有通知节点的未完成事项，呈现方式由渠道决定','automation-boundary'));
      field(fields,settings,'enabled','boolean',{label:'启用每日早报'});field(fields,settings,'time','string',{label:'发送时间（HH:MM）'});field(fields,settings,'timezone','string',{label:'早报时区'});field(fields,settings,'catch_up_minutes','integer',{label:'补发窗口 · 分钟',min:1,max:1440});
    }
    const interval=grid(details(reminderAdvanced,'无截止时间时的间隔提醒 · 秒（0 不重复）'));for(const key of ['P0','P1','P2','P3','P4'])field(interval,config.messaging.reminders,key,'integer',{min:0,max:31536000});
    const retry=details(byId('delivery-advanced'),'单条投递与状态回写的重试上限');retry.append(node('p','整轮重试与单条消息、回写请求的重试分别设置','automation-boundary'));
    for(const section of ['schedule','feedback']){retry.append(node('h4',section==='schedule'?'消息投递':'操作回写'));const area=grid(retry);for(const key of Object.keys(config.messaging[section]).filter(key=>key!=='interval_seconds'))field(area,config.messaging[section],key,'integer',{max:key==='max_attempts'?20:key==='max_deliveries_per_round'?1000:86400});}
    const alerts=grid(details(byId('processor-advanced'),'运行失败通知'));
    field(alerts,config.automation.alerts,'enabled','boolean',{label:'启用运行失败通知'});field(alerts,config.automation.alerts,'target_id','string',{label:'接收通知的通道编号'});field(alerts,config.automation.alerts,'cooldown_seconds','integer',{label:'重复故障通知间隔 · 秒',max:2592000});field(alerts,config.automation.alerts,'recovery_enabled','boolean',{label:'恢复后发送通知'});
    config.messaging.targets.forEach(targetCard);byId('new-kind').replaceChildren();
    for(const [kind,description]of Object.entries(config.messaging.types||{})){const option=node('option',description.name||kind);option.value=kind;byId('new-kind').append(option);}
  }
  function collect() {
    if(!config)throw Error('请先重新加载设置。');
    for (const {input} of controls) if (!input.checkValidity()) {
      let parent = input.parentElement; while (parent) {if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement;}
      if(input.closest('#automation-channels'))activatePanel('automation-channels');else activatePanel('automation-collection');input.reportValidity(); input.focus(); throw Error('请修正标出的设置后保存。');
    }
    for (const {input, object, key, type, optional} of controls) {
      if (optional && !input.value.trim()) {delete object[key]; continue;}
      if (type === 'minutes') {
        const values = input.value.trim() ? input.value.trim().split(/[,，\s]+/).map(Number) : [];
        if (values.some(v => !Number.isFinite(v) || v < 0 || !Number.isInteger(v * 60))) throw Error(key + ' 请填写有效的非负分钟数，使用逗号分隔。');
        object[key] = values.map(v => v * 60); continue;
      }
      object[key] = type === 'boolean' ? input.checked : type === 'boolean-select' ? input.value === 'true' : type === 'integer' ? Number(input.value) : type === 'strings' ? input.value.split(/[\n,，]+/).map(x => x.trim()).filter(Boolean) : input.value.trim();
    }
    const {schemas, types, ...settings} = config.messaging;
    const messaging={...settings,targets:settings.targets.filter(target=>Object.hasOwn(types||{},target.kind))};
    return {version: config.version, automation: config.automation, messaging};
  }
  async function loadConfig() {
    if (dirty && !window.confirm('重新加载会放弃未保存的自动运行和通道设置。继续？')) return;
    const [settings,catalog,sources] = await Promise.all([request('/config'),request('/catalog'),request('/catalog',{},'/api')]);
    config=settings;jobCatalog=catalog.jobs;sourceDescriptions=sources.sources;
    for(const [name,description]of Object.entries(jobCatalog))jobNames[name]=description.name;
    renderConfig(); dirty = false; notice('设置已加载 · 未修改', false);
  }
  async function saveConfig(payload) {
    if (!config) throw Error('请先重新加载设置。');
    const result = await request('/config', {method: 'PUT', body: JSON.stringify(payload)});
    config = {version: result.version, automation: result.automation, messaging: result.messaging};
    renderConfig(); dirty = false;
    notice(result.operation ? '已保存，运行中的服务正在按新配置重新启动或停止。' : '已保存。服务尚未运行，可点击启动后台服务。');
    await refreshStatus();
    window.dispatchEvent(new Event('operations-updated'));
  }
  async function operate(path, message) {
    if (dirty) throw Error('请先保存设置或重新加载，再执行运行操作。');
    const result = await request(path, {method: 'POST'});
    notice(result.state && states[result.state] ? states[result.state] : message); await refreshStatus();
    window.dispatchEvent(new Event('operations-updated'));
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
    const service = snapshot.service || {}, active = service.running, stopped = ['stopped','not_running'].includes(service.state);
    byId('status-strip').dataset.state=service.state||'unknown';byId('service-detail').textContent=states[service.state]||service.state||'状态未确认';byId('service-verified').textContent=service.verified?'已核对当前后台进程':'进程状态尚未确认';
    byId('service-boundary').textContent=active?'关闭网页后，已启用的计划继续运行':stopped?'后台服务未运行；启用的计划暂不执行':'后台状态尚未确认，请刷新后核对计划是否运行';
    byId('service-status').textContent = `${active ? states[service.state] || '运行中' : states[service.state] || service.state || '未运行'}${service.pid ? ' · PID ' + service.pid : ''}${service.config_changed ? ' · 配置已变化，请重新启动' : ''}`;
    const jobs = byId('jobs'); jobs.replaceChildren();
    const checks = snapshot.checks || {};
    const warnings = {'Configuration changes require restarting automation; check performs no live API validation.': '保存设置会重新启动已运行的后台服务；配置检查不验证外部服务连接。'};
    const visibleWarnings = (checks.warnings || []).filter(text => text !== 'Disable matching console schedules before enabling automation collectors.');
    if (visibleWarnings.length) jobs.append(node('p', visibleWarnings.map(text => warnings[text] || text).join(' '), 'scope'));
    const entries = [...(snapshot.automation?.jobs || [])];
    if (snapshot.automation?.listener) entries.push({...snapshot.automation.listener, name: 'listener', enabled: checks.jobs?.listener?.enabled});
    table(jobs, ['任务', '状态', '失败尝试次数', '下次运行', '上次成功', '最近故障 / 配置检查'], entries.map(job => {
      const next = node('span', '—');
      if (active && job.enabled && job.next_run && job.state !== 'paused') next.dataset.opsNext = job.next_run;
      const check = checks.jobs?.[job.name];
      const failure = snapshot.automation?.service?.['last_failure:' + job.name];
      const detail = failure?.failures?.[0], frame = detail?.frames?.at(-1);
      let issue = job.active_error || job.error || (check?.ready === false ? check.reason : '') || '';
      if (detail) issue += `${issue ? ' · ' : '上次：'}${detail.code}${frame ? ` · ${frame.file}:${frame.line}` : ''}（${date(failure.at)}）`;
      let problem=issue||'—';
      if(issue){
        problem=node('div');button(problem,issue+' · 查看详情',async()=>{
          if(job.current_run_id){try{await showJob(job.current_run_id,'automation');return;}catch{}}
          const box=document.getElementById('job-detail');box.replaceChildren(node('h3',jobNames[job.name]||job.name),node('p','尚无可关联的运行记录。以下为当前状态与配置检查，可在自动化中修正配置。','scope'),node('pre',JSON.stringify({job,check,last_failure:failure},null,2)));
          const link=node('a','打开任务配置','button secondary'),category=['deliver','feedback'].includes(jobCatalog[job.name]?.action)||job.name==='listener'?'channels':'collection';link.href='#automation/'+category;link.addEventListener('click',()=>{document.getElementById('detail-dialog').close();location.hash='automation/'+category;showView();const target=[...document.querySelectorAll('[data-job]')].find(node=>node.dataset.job===job.name);if(target){let parent=target;while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}target.scrollIntoView({block:'center'});}});box.append(link);document.getElementById('detail-dialog').showModal();
        });
      }
      return [jobNames[job.name] || job.name, !job.enabled ? '未启用' : !active ? stopped ? '服务未运行' : '后台状态未确认' : states[job.state] || job.state, job.attempts ?? 0, next, date(job.last_success), problem];
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
  function renderStartup(value) {
    byId('startup-chip').textContent = !value.supported ? '系统不支持' : value.blocked_by_windows ? '系统已禁用' : value.installed && !value.matches ? '路径需核对' : value.enabled ? '已开启' : '未开启';
    byId('startup-status').className = value.supported && !value.blocked_by_windows && !(value.installed && !value.matches) ? 'sr-only' : '';
    for(const id of ['startup-enable','startup-disable']){byId(id).dataset.opsUnavailable=String(!value.supported);byId(id).disabled=!value.supported||busy;}
    byId('startup-status').textContent = !value.supported ? '此系统不支持 Windows 登录启动项。' : value.enabled ? '已开启：当前用户下次登录后自动启动。' : value.blocked_by_windows ? '已被 Windows 任务管理器禁用；点击开启可重新启用。' : value.installed && !value.matches ? '发现同名启动项，但启动路径不同；请核对安装目录后重新开启。' : '未开启';
  }
  async function refreshStartup() {
    try {renderStartup(await request('/startup'));}
    catch (error) {
      byId('startup-chip').textContent='状态未确认';byId('startup-status').className='';byId('startup-status').textContent='启动项状态读取失败：'+error.message;
      for(const id of ['startup-enable','startup-disable']){byId(id).dataset.opsUnavailable='true';byId(id).disabled=true;}
    }
  }
  for (const [id, enabled] of [['startup-enable', true], ['startup-disable', false]]) {
    byId(id).addEventListener('click', () => action(async () => {
      renderStartup(await request('/startup', {method: 'PUT', body: JSON.stringify({enabled})}));
    }));
  }
  byId('save').addEventListener('click', () => action(saveConfig));
  byId('reload').addEventListener('click', () => action(loadConfig));
  for (const command of ['start', 'stop', 'restart']) byId(command).addEventListener('click', () => action(() => operate('/service/' + command, '后台服务操作已提交。')));
  byId('refresh').addEventListener('click', () => {refreshStatus(); refreshStartup();});
  byId('add-target').addEventListener('click', () => action(async () => {
    if (!config) throw Error('请先加载配置。');
    const id = byId('new-id').value.trim(), kind = byId('new-kind').value;
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(id)) throw Error('通道编号只能包含字母、数字、下划线或连字符，最多 100 字符。');
    if (config.messaging.targets.some(target => target.id === id)) throw Error('通道编号已存在。');
    const description=channelDescription(kind);if(!description)throw Error('此通道类型没有可编辑描述');
    const target={...structuredClone(description.defaults),id,kind,enabled:false};
    config.messaging.targets.push(target);expandedTargets.add(id);targetCard(target);byId('new-id').value='';byId('target-dialog').close();markDirty();
  }));
  byId('open-add').addEventListener('click',()=>{byId('add-error').textContent='';byId('target-dialog').showModal();});
  byId('close-add').addEventListener('click',()=>byId('target-dialog').close());
  byId('close-rules').addEventListener('click',()=>{document.getElementById('automation-rules').hidden=true;});
  window.addEventListener('operations-updated',refreshStatus);
  window.addEventListener('beforeunload', event => {if (dirty) {event.preventDefault(); event.returnValue = '';}});
  let initialized=false;
  function enterAutomation(){if(document.hidden||document.getElementById('view-automation').hidden)return;if(!initialized){initialized=true;action(loadConfig);refreshStartup();}refreshStatus();}
  window.addEventListener('hashchange',enterAutomation);document.addEventListener('visibilitychange',enterAutomation);enterAutomation();
  setInterval(()=>{if(!document.hidden&&!document.getElementById('view-automation').hidden)refreshStatus();},5000);
  setInterval(()=>{if(!document.hidden&&!document.getElementById('view-automation').hidden)countdowns();},1000);
});
