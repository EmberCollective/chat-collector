# 待办状态反馈接口

消息层接收用户明确操作后，通过处理层公开函数更新待办状态或优先级，不能直接修改处理层 SQLite。两种操作均发布原有 `upsert` 契约；来源、附件、行动步骤和参与人员保持不变。

```python
from processor import load_config, update_task_status

result = update_task_status(
    load_config("config/processor.toml"),
    task_id="处理层返回的 task_id",
    status="completed",
    request_id="稳定且全局唯一的回调或操作编号",
    actor={"platform": "notion", "user_id": "已验证的用户 ID", "source": "页面或回调来源"},
    expected_revision=3,
)
```

| 参数 | 契约 |
| --- | --- |
| `status` | `completed`、`open`、`cancelled`；不接受任意字段更新或 SQL |
| `task_id`、`request_id` | 非空字符串，最多 200 字符，无控制字符；去除首尾空白 |
| `actor` | 只允许 `platform`（最多 64 字符）、`user_id`、`source`（各最多 200 字符），均非空、无控制字符 |
| `expected_revision` | 可选正整数，用于拒绝过期界面上的操作；布尔值不接受 |

成功返回 `request_id`、`task_id`、`status`、`revision`、`previous_status`、`changed`、`actor`、`recorded_at`。返回值是该次操作的确认记录；重复请求返回同一记录，即使任务后来已有其他修订，也不重新执行旧操作。

## 幂等、审计和错误恢复

`request_id` 是整个处理库的幂等键。同 ID、相同规范化内容重放不产生新修订；同 ID 但不同状态、任务、身份或预期修订被拒绝。失败后重试必须沿用原 ID，不能每轮随机换号。

处理层在同一 SQLite 事务保存任务修订、输出队列及 `task_status_requests` 审计记录。审计包含请求内容、操作者、修改前后状态、修订号和时间。事务成功后再导出 JSONL；导出失败不回滚已经确认的状态，同请求重试补完导出。无实际状态或人工控制变化的新请求仍保留审计，但不产生无意义的新任务修订。

| 异常 | 含义与消息层处理 |
| --- | --- |
| `TaskStatusBusy` | 处理层正在执行另一项操作；立即返回，消息层持久队列稍后重试，不阻塞 AI 运行 |
| `TaskStatusExportPending` | 状态与审计已持久化，JSONL 导出未完成；沿用同请求 ID 重试 |
| `TaskStatusConflict` | 幂等键内容冲突、预期修订过期或任务已被替代；需获取最新状态，不能自动覆盖 |
| `UnknownTask` | 任务不存在；不能据此创建新待办 |
| `ValueError` | 参数不符合契约，修正请求后再调用 |
| `OSError` / `sqlite3.Error` | 本地存储故障；保留同请求 ID，由队列稍后重试或报告错误 |

接口复用处理层实例锁，默认不等待。回调方需要先校验外部签名、授权和操作者，再形成 `actor`；本地函数不会验证远端身份，也不会把一个声称的 `user_id` 当作已认证事实。

## 人工状态与后续 AI 更新

优先级使用 `processor.update_task_priority(config, task_id, priority, request_id=..., actor=..., expected_revision=...)`，只接受 `P0`–`P4`。身份、幂等、版本检查、实例锁、失败重试与状态接口相同，共用 `task_status_requests` 审计表和 outbox。返回字段使用 `priority`、`previous_priority`，其余字段与状态接口一致。

人工优先级保存为 `priority_control`，包含级别、操作者、操作编号和时间，`priority_reason` 标记为用户手动设置。模型后续更新同一待办时保留此级别及说明；再次人工调整可覆盖。它不修改完成状态。消息层消费新修订后淘汰旧修订的未发投递，更新已有卡片，按新级别安排后续节点，不额外补发过去的提醒。人工控制按 `task_id` 保存，不宣称可以自动识别所有语义相同但被模型误建为不同编号的事项。

人工完成或取消写入任务的 `status_control`。模型后续复用同 `task_id` 时可以补充内容、来源和材料，但不能自动重开人工关闭的事项；重新分组也不把这类历史记录自动替代。只有用户通过该接口明确设为 `open` 才释放人工关闭状态。

查重公开接口与 Pi `find_related_tasks` 支持 `statuses=["completed"]`，默认查询也包含已完成事项，避免模型因看不到历史而重复创建。Pi 的 `submit_tasks` 仍只允许 `open` / `cancelled`，模型不能代用户确认完成。

## 消息层反馈队列

`messaging.feedback` 使用独立 SQLite 接收反馈，不直接写处理层数据库。回调验签及身份授权完成后调用：

```python
from messaging import enqueue_feedback, process_pending

queued = enqueue_feedback(
    config, "feishu-personal", "平台的稳定事件编号", task_id,
    "complete", {"platform": "feishu", "user_id": "已验证的用户 ID", "source": "card"},
    expected_revision=3,
)
# 由后台自动化调用，不在回调 HTTP 请求里执行。
summary = process_pending(config)
```

`action` 支持 `complete`、`reopen`、`cancel`、`snooze`、`set_priority`。`set_priority` 必须额外提供 `priority="P0"`–`"P4"`；其他动作不接受此参数。入队只执行超时上限为 1 秒的 SQLite 短事务，不等待处理层或消息层实例锁、不扫描批次。事件编号和目标共同派生处理层请求 ID；重复内容返回原来的入队确认，不再次应用动作，同事件不同内容拒绝。授权及平台签名校验属于调用方职责。

配置位于消息层配置的 `[feedback]`：

```toml
[feedback]
enabled = false
state_file = "../data/messaging/feedback.db"
processor_config = "processor.toml"
retry_base_seconds = 60
max_attempts = 5
```

这些路径相对消息层配置文件。反馈库可与飞书专用 `feishu_*` 表共存，不能指向处理库、消息库或其他应用库。反馈重试参数独立于外发投递重试。`snooze` 的截止点在入队时固定，采用对应目标的 `snooze_seconds`，默认 3600 秒；重试不向后推迟。过期后才处理到的稍后提醒不会重新延长时间。

后台先调用处理层公开 API，再同步已发布的任务事件，最后按正式状态更新消息层的完成标记。两库之间没有共享事务：任一阶段中断后沿用同请求 ID 重试，处理库已接受的请求不会重复递增修订。同任务按入队顺序处理，正在重试的旧动作不会被新动作越过。处理层忙时不消耗失败次数；其他故障指数退避，达到上限后报告失败。修订冲突和未知任务明确报告，不自动覆盖新状态。

`process_pending` 返回 `applied`（本轮成功数）及当前 `retry`、`failed`、`conflict`、`pending` 数量，供自动化报警。队列仍有未完成、失败或冲突的反馈时，暂停该任务的新催办及尚未发送的旧投递；已经发出的网络请求无法撤回。新的明确操作成功后，可以取代此前失败或冲突的操作并解除暂停。

本地工具用 `request_task_action` 走同一队列；未开启反馈时，旧的本地完成/重开/稍后提醒仍可用。命令行：

```powershell
python -m messaging complete --config config/messaging.toml --task-id TASK --event-id LOCAL_OPERATION
python -m messaging feedback-status --config config/messaging.toml
python -m messaging retry-feedback --config config/messaging.toml --request-id REQUEST
python -m messaging once --config config/messaging.toml
```

`once` / `run` 先处理反馈，再执行正常消息轮次。`retry-feedback` 保持原请求内容；修订冲突需要基于最新状态发起新操作，而不是改写已存在的请求。同步或导出故障修复后可直接重试原请求。队列与成功结果留存用于审计，没有自动删除历史记录。
