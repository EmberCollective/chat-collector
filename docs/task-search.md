# 待办数据库查询

`find_related_tasks` 通过 Pi 原生工具请求回调 Python，实时只读查询已保存的完整待办库。它不再筛选预加载的最近 40 项列表，不读取原始聊天数据库，也不把整个待办库压入初始上下文。无需新增配置，正式运行使用 `storage.state_file`。

## 参数

全部参数可选，不传范围时覆盖完整待办库。不同字段之间为 AND；同一列表内的值为 OR。

| 参数 | 含义 |
|---|---|
| `query` | 标题或事件正文的字面子串，忽略大小写；`%`、`_` 不是通配符 |
| `platforms` | 平台列表，如 `["qq", "wechat"]`；匹配原始归属或保留的消息来源 |
| `account_ref` | 本轮查询结果返回的账号短引用，如 `a1` |
| `conversation_ref` | 本轮输入或查询结果中的会话短引用，如 `c1` |
| `people` | 已保存待办的关联人物姓名子串列表，不是发信人或通讯录搜索 |
| `person_role` | 人物角色子串，如“负责人”；与姓名条件必须命中同一个人 |
| `statuses` | `open`、`cancelled`、`superseded`；默认排除 `superseded` |
| `priorities` | `P0` 到 `P4` 的列表 |
| `needs_review` | 是否需要复核，布尔值 |
| `time_field` | `deadline`（默认）、`start`、`updated_at`、`source_time`（来源消息发送时间） |
| `time_from` / `time_to` | 时间起点包含、终点不包含；接受日期或带时区的 ISO 时间 |
| `sort_by` / `order` | 按 `updated_at`（默认）、`deadline` 或 `start` 排序；`desc`（默认）或 `asc`；空时间排末尾 |
| `limit` / `offset` | 默认 20 / 0，单页最多 100；返回 `has_more`、`next_offset` |

文本筛选最多 200 字符，列表最多 20 项。日期按处理配置的时区解释为当天零点；完整时间必须有时区偏移。时间字段为空的待办不会匹配时间范围。`source_time` 表示任一来源消息在范围内，不是所有来源都必须在范围内。

```json
{
  "platforms": ["wechat", "qq"],
  "query": "方案",
  "people": ["张老师"],
  "person_role": "负责人",
  "time_field": "deadline",
  "time_from": "2026-11-01",
  "time_to": "2026-12-01",
  "statuses": ["open"],
  "priorities": ["P1", "P2"],
  "sort_by": "deadline",
  "order": "asc",
  "limit": 20,
  "offset": 0
}
```

`has_more=true` 时保持其他条件，使用 `next_offset` 查询下一页。分页按指定时间和稳定任务 ID 排序。查询期间其他运行若修改正式库，后续页可能发生变化；查重应结合任务短引用，不能把某一页当成全库结果。

## 结果和更新

每条结果包含任务短引用、标题、事件、优先级、状态、时间、人员、地点、复核状态、更新时间、来源平台与账号/会话短引用，以及 `updatable`。真实任务 ID、数据库路径和来源消息正文不会随搜索结果返回。重复查询同一任务使用同一个短引用。

`submit_tasks` 可以引用搜索得到的任务更新；程序将其映射为真实 ID，并保留原有来源、附件证据和原始所属会话，再加入当前会话证据。新的模型证据必须来自本批一个明确会话。显式查询到的 `superseded` 任务仅可查看，不能复活更新。

查重不等于标题相同即合并：每周同名任务、同名人物不能自动认定为同一事项。人物资料缺失时姓名筛选可能漏掉候选，应扩大条件查询。不提供向量搜索或同义词扩展，最终语义关联仍由模型判断。

## 调试和开发

调试执行以正式库作为只读查询来源，并叠加本轮调试库的更新（同 ID 以调试修订为准）。提交只写调试库，不修改正式待办或断点。旧调试目录不会被自动纳入正式查询。

本地代码可调用 `processor.search_tasks(config, ...)`，它返回完整本地任务；这个开发接口的账号和会话筛选用 `account_id`、`conversation_id`。Pi 经 `TaskLookup` 转换为短引用和精简字段。SQL 参数全部绑定，排序字段取白名单；每次查询有 5 秒执行预算，错误不返回本地数据库路径。

实现位于 `processor/task_search.py`，桥接位于 `processor/pi_bridge.py` 与 `processor/pi-runtime/runner.mjs`。没有新增模型循环、压缩机制或任意 SQL 工具。
