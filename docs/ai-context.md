# AI 输入契约：ai-context.v1

输入由已经符合 [数据契约](data-contract.md) 的消息投影而来。原始数据库适配、名称解释、消息类型判断都在提供方；处理层只分配短引用并组合已选片段。完整合成样本见 [ai-context-v1.json](examples/ai-context-v1.json)。

## 输入结构

| 层级 | 字段与含义 |
|---|---|
| 根 | `schema_version="ai-context.v1"`、`timezone`、`accounts`、`conversations`、`attachments` |
| `accounts[]` | `ref`、`platform`、`label`、`self:{ref,nickname}`，说明数据属于谁的账号 |
| `conversations[]` | `id`、`platform`、`account_ref`、`type`、`name`、`remark`、`people`、`segments` |
| `people[]` | `ref`、`display_name`、`nickname`、`remark`、`group_nickname`、`is_self`、`missing_fields` |
| `segments[]` | `id`、按提供方顺序排列的 `messages`；片段之间不假设连续 |
| `messages[]` | `id`、带时区的 `time`、`sender_ref`、人类可读 `sender`、`role`、`text`、`kind`、`mentions_all`、可选 `attachments` |
| `attachments[]` | 短引用 `ref`、名称、类型、可读状态和读取能力；初始无正文 |

当前字段命名沿用现有工具：会话、片段、消息用 `id`，账号、人物、附件用 `ref`。不要把讨论稿中的 `message.ref`、`attachment_refs` 直接当作当前字段。

`sender` 来自提供方生成的名称标签，便于直接阅读；`sender_ref` 对应本会话 `people[].ref`，用于消除同名歧义。每个会话的人物表只包含选中消息涉及的人。人物资料可以重复出现在不同会话，以保留不同群昵称；同平台、同账号、同一人使用相同短引用。当前账号自己未发言时，自身资料仅留在 `accounts.self`。

## 聊天人物与任务人员

`conversations.people` 是资料，不是待办负责人名单。`submit_tasks.tasks[].people` 是模型有依据提取的负责人或参与人，仍为 `{name,role}`。发送消息、被 @、被备注为“老师”，都不能单独证明承担任务。

未知身份不冒充已知人物；缺失名称用 `null` 与 `missing_fields` 表示。同名不跨账号或跨平台自动合并。当前任务人员尚未升级为稳定人员 ID，相关姓名搜索的限制见 [待办查询](task-search.md)。

## 触发消息仍是普通消息对象

```json
{
  "id": "m6",
  "time": "2026-11-12T18:59:18+08:00",
  "sender_ref": "u2",
  "sender": "张老师（昵称：张三）",
  "role": "history",
  "text": "",
  "kind": "file",
  "mentions_all": false,
  "attachments": ["f1"]
}
```

`history` 表示它早于本轮处理起点，不表示它不可能是上下文规则的中心。`current` 也不意味着直接命中了文件或关键词规则。文件没有正文仍保留 `kind` 和附件索引；全员提及保留结构化标志。`matched_rules` 留在本地来源中，不把规则名误当成消息类型。

## 本地来源与模型资料的边界

本批短引用 `a/c/u/m/f/t` 映射由程序维护；下一批可能重新编号。真实账号 ID、消息 ID、hash、数据库路径、原表、完整来源映射、名称采集日志、模型提交留存和审阅记录不注入初始上下文。

消息与身份扩展默认不透传；不能把任意 JSON、草稿或完整通讯录放进系统提示词。用户 Markdown 是指令；聊天、附件、名称、工具返回和已有待办都是资料，不能覆盖指令。Pi 原生会话和压缩负责运行中的上下文管理。

已有待办只在 `find_related_tasks` 调用时按查询页返回；附件正文只在 `read_attachment` 调用时返回。待办查询、附件读取和最终提交的参数分别见 [待办查询](task-search.md)、[附件读取](attachment-context.md)、[Pi 工具](pi-runtime.md)。

最终 `sources:["m1","f1:p2"]` 由程序映射回消息修订、平台、账号、人物、附件页和本地跳转。未读取附件页不能被引用。修改同一任务时保留旧证据；跨平台语义关联需要模型判断，不能按人名相同自动去重。

## 分组与兼容

允许一批多个会话，保留各自边界；配置可强制单会话批次。超长时按完整已选片段分批，不截断一个关联片段。字符预算不足则明确报告，而不是隐藏删消息。身份表增加了输入长度，预览以实际生成的 payload 为准。

旧消息缺少身份快照时，输入仍有明确的人物短引用和缺失名称，不在处理层访问平台数据库补人名。需要新资料时应在提供方重放或重新预览。已有待办来源快照不会因界面更新而自动补成新姓名。
