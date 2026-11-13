# 消息层：行动包、材料和提醒

消息层位于 `messaging/`，只消费 `config/messaging.toml` 指定的正式待办 JSONL。它不导入 `processor`，不调用模型，也不刷新原聊天库。复用现有文件锁和序列化工具；状态、材料、投递队列均由本层维护。调试台只是调用公共函数的可选客户端。

## 输入与输出契约

输入沿用处理层已发布的 `schema_version=1` 任务事件：

```json
{
  "schema_version": 1,
  "event_id": "稳定事件ID",
  "operation": "upsert",
  "task_id": "稳定待办ID",
  "revision": 2,
  "task": {
    "task_id": "稳定待办ID",
    "revision": 2,
    "title": "填写并提交申请表",
    "event": "按通知要求完成申请表并提交到指定入口。",
    "action_steps": ["填写申请表", "核对材料并提交"],
    "priority": "P2",
    "priority_reason": "有明确期限的常规申请",
    "status": "open",
    "time": {"start": null, "deadline": "2026-11-20", "timezone": "Asia/Shanghai", "text": "11月20日前"},
    "people": [{"name": "申请同学", "role": "参与人"}],
    "location": null,
    "needs_review": false,
    "review_reasons": [],
    "sources": [],
    "materials": [],
    "attachment_evidence": []
  }
}
```

上例省略了实际来源；生产事件中的 `sources` 由处理程序从提供方事实生成，`materials` 与 `attachment_evidence` 包含 `{message_key, attachment_index, attachment}`。模型只提交短引用，不生成本地归档路径。`people` 是需要参与事项的人，负责人只是明确指派后的其中一种角色。

本层在输入边界校验结构。事件 ID 相同、内容不同或任务同修订内容不同均拒绝导入。多条旧修订可以乱序到达，最终任务只保留最高修订。`.tmp` 不读取，已经导入的 JSONL 必须保持不可变。坏批次不跳过后假装成功，应先检查并修复输入。

输出行动包包含稳定任务身份、执行步骤、参与人员、时间、地点、复核提示、精简来源和材料索引。外部 `envelope` 额外提供 `delivery_id` 与 `kind=upsert|reminder`。字段映射和回执示例见 [渠道契约](messaging-channels.md#外部工作流契约)。不额外建立 AI 信息处理层；信息整理和渠道映射是确定性的。

## 材料生命周期

1. 从 `materials` 和确实被引用的附件证据建立执行材料清单。同一来源消息的其他附件不自动发送。旧任务只有消息来源、没有材料关联时保留未选择数量；重新处理可补齐关联，不偷偷上传全部附件。
2. 预览只读校验已有归档；本地 `sync` 按允许目录和 SHA-256 校验后复制到消息层内容寻址目录，不使用指向可变原件的硬链接。
3. 同内容跨任务复用一个文件，清单保留各自名称、来源、用途和角色。数据以临时文件写入并校验后原子发布，校验失败不作为可读材料外发。
4. 缺原件、密文、无法验证的附件显式不可用。本层不再自行解密；附件读取/解码仍走上游提供方，补齐后任务新修订或同修订可见材料变化会触发资料同步。
5. 用户可用 `attach-result` 显式选择成果文件，`role=result` 和说明保存在独立结果表。完成、取消或后续修订不会删除它们。
6. 下载只接受已有任务和材料编号，重新核对路径范围和文件哈希。外部请求不接受任意本地路径；远程部署需由接入模块上传材料到目标平台或对象存储。

默认目录 `data/messaging/assets/<sha256前两位>/<sha256>`，名称不作为存储路径。归档文件保留到用户另行管理；当前无自动垃圾回收，防止完成后误删证据。备份应同时包含 `data/messaging/state.db`、材料目录和上游正式待办 JSONL。Notion 上传收据与本地材料分离；外部副本同样不会因为本地完成而删除。

## 提醒与恢复

`schedule.interval_seconds` 是检查间隔；`reminders.P0..P4` 是上次成功投递后重复提醒间隔，两者互不替代。`0` 表示不重复提醒。机器休眠/关机不会发送，恢复后最多补一条当前提醒，不补发每个错过的周期。初版没有静默时段或手机推送可靠性承诺，可由外部工作流补充。

Notion 和 `mode="sync"` 的目标只同步内容或状态变化。初次本地已经完成的任务仍可进入资料库；通知渠道不首次催办已结束任务。完成、取消、替代停止周期提醒；稍后提醒抑制催办。新处理修订不会默默清除用户的完成标记；需要用户显式恢复。

每次请求前持久化 `sending`，成功后保存回执。网络断开或进程崩溃无法确认远端是否成功时标记 `unknown`，同任务/目标后续投递暂缓；明确拒绝则指数退避，达到上限保留 `failed`。SQLite 事务无法包住远程请求，所以不承诺全平台恰好一次。

```powershell
python -m messaging status
# 核对远端后，明确选择重试或确认已送达；重试不更换 delivery_id
python -m messaging resolve-delivery --delivery-id "投递ID" --resolution retry
python -m messaging resolve-delivery --delivery-id "投递ID" --resolution delivered
```

Notion 需要页面回执，不能缺回执直接标记送达；重试时适配器先按 Task ID 查询对账。通用接入方应将 `delivery_id` 去重和远程对象映射持久化，再返回成功。外部手动完成、日历改期目前不会自动回写本地；扩展时通过本层状态接口处理，不让渠道直接修改处理层数据库。

## 公共入口

```python
from messaging import load_config, preview, sync, run_round, status
from messaging import set_task_state, add_result, task_package

config = load_config('config/messaging.toml')
preview(config)                         # 无写入、无网络
sync(config)                            # 导入与归档，无网络
run_round(config)                       # 只有已启用目标才可能投递
set_task_state(config, task_id, 'complete')
add_result(config, task_id, file_path, note='填写后的申请表')
package = task_package(config, task_id)
```

可选面板提供 `/api/messaging/status`、`preview`、`sync`、`tasks/{id}/state` 与 `tasks/{id}/materials/{resource_id}`。复用本机来源检查与 `X-Console-Token`，面板不提供外部发送按钮。本地调试令牌不是适合公网部署的长期访问凭据。

扩展渠道优先注册 `kind="webhook"` 接 n8n 或已有模块，复用稳定契约；确需进程内插件时，在 `channels.deliver` 加显式分发与对应测试。只有本地配置决定运行什么代码，消息正文不能选择插件。新增提供方无需修改处理规则、采集 SQL 或 Pi 工具。

验证覆盖事件重复/乱序/冲突、来源状态库隔离、提醒间隔与完成状态、素材校验及存续、投递退避/未知结果恢复、真实 Notion SDK 的本地模拟响应。尚未配置的外部账户与 OAuth 工作流需另行联调。
