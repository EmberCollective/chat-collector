'use strict';
// No external sends here: local sync and user state changes only.
document.addEventListener('DOMContentLoaded', () => {
  const area = document.getElementById('messaging-output');
  const notice = document.getElementById('messaging-notice');
  let busy = false;
  const node = (tag, text) => {const n = document.createElement(tag); if (text != null) n.textContent = text; return n;};
  const duration = seconds => seconds % 86400 === 0 ? seconds / 86400 + ' 天' : seconds % 3600 === 0 ? seconds / 3600 + ' 小时' : seconds / 60 + ' 分钟';
  const states = {open: '待处理', completed: '已完成', done: '已完成', cancelled: '已取消', superseded: '已替代'};
  async function request(path, options = {}) {
    const session = await fetch('/api/session').then(r => r.json());
    const response = await fetch('/api/messaging' + path, {...options, headers: {'X-Console-Token': session.token, 'Content-Type': 'application/json'}});
    if (!response.ok) throw Error((await response.json()).detail || '操作失败');
    return response;
  }
  async function action(callback) {
    if (busy) return;
    busy = true;
    document.querySelectorAll('#messaging-panel button').forEach(b => b.disabled = true);
    try {await callback();} catch (e) {notice.textContent = e.message;} finally {
      busy = false;
      document.querySelectorAll('#messaging-panel button').forEach(b => b.disabled = false);
    }
  }
  async function load() {
    const [preview, status, feedback] = await Promise.all([request('/preview').then(r => r.json()), request('/status').then(r => r.json()), request('/feedback').then(r => r.json())]);
    notice.textContent = `共 ${preview.total_tasks} 项；待导入 ${preview.pending_events} 项更新。外部投递${status.enabled ? '已在配置中启用（需独立运行消息层）' : '关闭'}。`;
    const summary = node('p', Object.entries(status.reminders).map(([p,s]) => `${p}：${s ? duration(s) : '不重复提醒'}`).join(' · '));
    summary.className = 'scope'; area.replaceChildren(summary);
    const pending = (feedback.requests || []).filter(r => !['applied', 'superseded'].includes(r.status));
    if (pending.length) area.append(node('p', `有 ${pending.length} 项操作等待回写或恢复；这些待办已暂停提醒。请保持反馈处理程序运行。`));
    if (!preview.tasks.length) area.append(node('p', '尚无正式待办输出。调试处理结果不会自动投递；正式处理后可在此预览。'));
    for (const task of preview.tasks) {
      const card = node('article'); card.className = 'message task';
      card.append(node('h3', `${task.priority} · ${task.title}`), node('p', task.event));
      const people = (task.people || []).map(p => [p.name, p.role].filter(Boolean).join(' · ')).join('、');
      card.append(node('p', `参与人员：${people || '未明确'} ｜ 截止：${task.time?.deadline || '未明确'} ｜ 状态：${states[task.status] || task.status}`));
      if (task.action_steps?.length) {const list = node('ol'); for (const step of task.action_steps) list.append(node('li', step)); card.append(list);}
      const notes = task.notes || task.review_reasons || [];
      if (notes.length) {const info = node('p', '说明：' + notes.join('；')); info.className = 'muted'; card.append(info);}
      const materials = node('ul');
      for (const resource of task.resources || []) {
        const item = node('li'); item.append(node('span', `${resource.role === 'result' ? '成果' : '材料'} · ${resource.name} `));
        if (resource.type === 'link' && /^https?:\/\//i.test(resource.url || '')) {const link = node('a', '打开链接 ↗'); link.href = resource.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link);}
        else if (resource.status === 'ready') {
          const button = node('button', '下载'); button.className = 'text-button';
          button.addEventListener('click', () => action(async () => {
            const response = await request(`/tasks/${encodeURIComponent(task.task_id)}/materials/${encodeURIComponent(resource.id)}`);
            const url = URL.createObjectURL(await response.blob());
            const a = node('a'); a.href = url; a.download = resource.name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
          })); item.append(button);
        } else item.append(node('span', '本地原件不可用'));
        if (resource.warnings?.length) item.append(node('span', ' · ' + resource.warnings.join('、')));
        if (resource.note) item.append(node('p', resource.note));
        materials.append(item);
      }
      card.append(materials);
      if (task.unselected_materials_count) card.append(node('p', `另有 ${task.unselected_materials_count} 个来源附件未被选为此待办材料，因此不自动上传。旧待办可重新处理以补全材料关联。`));
      if ((task.sources || []).length) {
        const sources = node('details'); sources.append(node('summary', '信息来源与原文'));
        for (const source of task.sources) {
          sources.append(node('p', [source.platform, source.conversation, source.sender, source.time].filter(Boolean).join(' · ')));
          const original = node('p', source.text || '（无文字正文）'); original.style.whiteSpace = 'pre-wrap';
          sources.append(original);
        }
        card.append(sources);
      }
      const feedbackStates = {queued: '操作已排队', applying: '正在保存操作', retry: '操作等待重试', failed: '操作保存失败', conflict: '内容已变化，操作未应用'};
      const currentFeedback = pending.find(r => r.task_id === task.task_id);
      if (currentFeedback) card.append(node('p', feedbackStates[currentFeedback.status] || currentFeedback.status));
      const buttons = node('div'); buttons.className = 'actions';
      for (const [label, type] of (task.status === 'open' ? [['标记完成', 'complete'], ['1 小时后提醒', 'snooze']] : task.status === 'completed' ? [['恢复待办', 'reopen']] : [])) {
        const b = node('button', label); b.className = 'secondary';
        b.addEventListener('click', () => action(async () => {
          const body = {action: type}; if (type === 'snooze') body.until = new Date(Date.now() + 3600000).toISOString();
          await request(`/tasks/${encodeURIComponent(task.task_id)}/state`, {method: 'POST', body: JSON.stringify(body)}); await load();
        })); buttons.append(b);
      }
      card.append(buttons); area.append(card);
    }
    if (preview.has_more) area.append(node('p', '当前显示前 20 项，完整结果可通过消息层 API 或命令行读取。'));
    const details = node('details'); details.append(node('summary', '投递记录'), node('pre', JSON.stringify(status.deliveries, null, 2))); area.append(details);
  }
  document.getElementById('messaging-preview').addEventListener('click', () => action(load));
  document.getElementById('messaging-sync').addEventListener('click', () => action(async () => {await request('/sync', {method: 'POST'}); await load();}));
});
