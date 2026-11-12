# 收集层 → 处理层数据契约

处理层要求提供方交付已经解释好的消息、身份与附件索引。原始 SQL、数据库版本、平台字段、密钥、文件定位与解码由提供方负责；处理层校验契约、组装 AI 输入、调用工具、保存待办和证据。采集与筛选不使用 AI。

## 当前版本与边界

| 契约部分 | 当前状态 |
|---|---|
| `schema_version: 1` 消息事件、JSONL 批次 | 已实现，现有消费者入口 |
| 消息内 `identity`、人类可读 `sender` | 已实现，按选中上下文提取；名称缺失明确保留 |
| AI 输入 `ai-context.v1` | 已实现，见 [AI 输入契约](ai-context.md) |
| 平台 `extensions` | 可以随来源记录保留，不自动送给 AI；尚无独立扩展字段校验器 |
| `context_notes` | 已确认的扩展方向，当前不自动注入 AI |
| `ingest_batch` / 通用提供方注册 API | 架构约定，尚未作为可调用公开 API 实现；当前通过 JSONL 和模块配置接入 |
| 附件按编号自动准备与读取 | 已实现；当前桥接仍在 `processor/attachment_resolver.py` 调用采集能力 |

本文区分现有入口和后续接口目标，不能将尚未实现的方法当成 SDK 调用。提供方的数据库不需要具有相同表结构；需要一致的是交付契约。

## 事件信封

每个 UTF-8 JSONL 行是一条完整事件。仅消费 `.jsonl`；忽略 `.tmp`。已发布文件不可修改。同目录可包含多个平台、多个账号的批次。

| 字段 | 类型与要求 |
|---|---|
| `schema_version` | 整数 `1`；未知主版本拒绝处理 |
| `event_id` | 非空字符串，同一消息修订的稳定事件标识 |
| `message_key` | 非空字符串，消息永久身份，重放和名称变化时保持不变 |
| `revision` | 从 1 开始的正整数，同一消息只接受更新修订 |
| `operation` | 当前仅支持 `upsert`，不将缺失记录推断为撤回/删除 |
| `payload_sha256` | 收集器生成的语义内容摘要，用于自身去重；不传 AI |
| `collected_at` | 带时区的 ISO 采集时间，内置收集器使用 UTC |
| `window` | `start_inclusive`、`end_exclusive`，Unix 秒，表示本轮扫描窗口 |
| `matched_rules` | 实际命中的 SQL 注册项数组，不能只保留最后一个规则 |
| `message` | 下述标准消息 |

完整合成样本：[provider-event-v1.json](examples/provider-event-v1.json)。这是格式示例，不是可投递到真实目录的生产事件。

`matched_rules` 每项包含 `file`、`params`、`recent_count`、`sql_sha256`。它表示本轮哪些 SQL 实际返回了该消息，不表示这条消息在历史每一轮曾命中过哪些规则。规则来源留在本地证据中，默认不注入 AI。

## 标准消息

| 字段 | 要求 |
|---|---|
| `platform` / `account_id` | 非空字符串，标识平台及当前采集账号 |
| `conversation_id` / `conversation_type` | 稳定会话 ID；内置模块为 `group` 或 `direct` |
| `message_id` | 平台稳定消息 ID；缺失时使用可复现、不会随重放改变的本地组合身份 |
| `sender_id` | 字符串；无法识别时为空，不能用群 ID 冒充发送者 |
| `sender` | 提供方生成的显示文字，例如“张老师（昵称：张三；群昵称：项目负责人）” |
| `sent_at` | Unix 秒整数，不与毫秒混用 |
| `text` | 可见正文；文件、图片等允许为空，不能捏造正文 |
| `kind` | 如 `text`、`file`、`image`、`voice`、`video`、`reply`、`forwarded`、`unknown` |
| `mentions_all` | 确认的平台全员提及标志；不能仅凭正文包含“@所有人”认定 |
| `attachments` | 本消息的附件索引数组，顺序稳定 |
| `identity` | 提供方已解析的账号、会话与相关人物资料 |
| `source` | 本地追溯信息：数据库、原表、原行定位、筛选连续性等，不发送给 AI |
| `metadata` / `extensions` | 本地保留的解析状态、平台扩展，不自动进入 AI 输入 |

`mentioned_user_ids`、`reply_sender_id` 可由提供方补充明确的结构化身份关联，用于限定人物范围。内置适配器尚未完整导出所有平台的单人 @、引用关系；不得从普通文本猜出这些 ID。

## 身份资料：仅限本次上下文

`identity` 是每条消息的事实快照。处理层只做去重、短引用替换及字段投影，不连接联系人数据库或推断昵称。

| 对象 | 字段 |
|---|---|
| `identity.account` | `id`、`nickname`、`label`、`source`、`observed_at` |
| `identity.conversation` | `id`、`type`、`name`、`remark`、`display_name`、`missing_fields`、`source`、`observed_at` |
| `identity.people[]` | `id`、`nickname`、`remark`、`group_nickname`、`display_name`、`label`、`is_self`、`missing_fields`、`source`、`observed_at` |
| `identity.warnings` | 可选，例如联系人库缺失；不把失败伪装成“该人没有昵称” |

允许提供的人物仅包括：选中消息的发送者、明确的 @ 对象、结构化引用关联的人、私聊对端。当前账号的自身资料单独放在 `account`；若自己也参与这些消息，再出现在 `people` 中。`@所有人` 不意味着展开所有群成员。不得导出完整通讯录或整群成员名单。

同一人员在不同群的 `group_nickname` 分别保存；备注属于当前账号的视角。账号范围内不同 ID 即使同名也不能合并。QQ 的 UID/QQ 号映射必须有源数据依据，不能仅比较字符串或名称。

名称使用字符串或 `null`。`display_name` 优先为备注、群昵称、平台昵称，再使用来源明确但类别未确定的显示名；`label` 由提供方组合其余不同名称。没有可靠名称时使用 `null` 和 `missing_fields`，显示为“名称未获取”。当前 QQ 私聊字段只能确认显示名时，不冒充独立昵称或备注。自定义账号标签配置为 `source.account_label`。

`source`、`observed_at` 描述名称资料的读取依据及快照时间，不证明这是消息发送时的历史昵称。当前系统没有完整名称变更历史，不能恢复后补资料之前的每个历史名称。

## 时间、连续性与触发消息

各规则独立查询同一轮标准消息表，合并去重后交付；不先用一个规则消费掉其他规则的输入。详见 [SQL 规则](../FILTERS.md)。

普通规则查询 `candidates`，仅包含本轮 `[start,end)` 消息。`context.sql` 查询含有限历史的 `messages`，以文件或全员提及为中心，保留同会话前后 N 条已知消息。新消息进入旧触发点的邻域时，可以再次选中整段；消息重复由事件层去重。

例如扫描 21:19–21:31，18:59 的文件后第 5 条消息在 21:28 到达：旧文件及前后文均由 `context.sql` 返回，但旧文件不在本轮 `candidates`，因此不会同时标记本轮 `files.sql`。文件仍包含在交付内容中，不是被丢弃。AI 输入保留 `kind=file`、附件索引与 `mentions_all`，避免空正文掩盖消息类型。

`source.selection_context.previous_message_key` 和 `previous_known` 来自提供方掌握的同会话顺序。处理层仅在前驱恰好存在于选中集合时拼接连续片段，不凭时间接近填补 SQL 缺口。`history/current` 表示时间资格，不是“上下文/触发点”标签；历史文件同样可以是触发点。

迟到入库的旧时间戳消息、旧内容修改，当前需要显式重放对应时间窗。不要把刷新间隔误认为自动回溯长度。

## 附件契约与工具边界

每个附件保留 `type`、`name`、可用时的 `size_bytes`、`md5`、平台定位信息；归档后提供 `archive_path`、`sha256`、归档状态及必要质量信息。平台定位、密钥和原始路径只在本地传递。消息归属和附件下标必须稳定，不能用同名文件直接认定同一附件。

AI 只使用本批 `f1` 等短引用调用 `read_attachment(ref,pages?,query?)`。当前链路为：验证归档 → 若缺失，调用采集适配器定位及归档/解码 → 公共解析器生成文本或图片 → Pi 原生工具内容。已有有效归档直接复用；定时采集的 `metadata` 强度不阻止这次显式读取。主动关闭附件归档仍被尊重。

已确认的分层目标是由提供方统一承担文件准备和解析，处理层只包装证据与 Pi 响应。现有实现仍在处理包内编排公共解析器，尚未完成通用提供方接口迁移；新增平台不能假设 `prepare_attachment` 已可直接注册。当前格式支持、页码、失败状态与限额以 [附件读取](attachment-context.md) 为准。

找不到本地文件、格式不支持、名称关联不明确、解码失败均返回明确状态；不伪造空文件内容，不要求 AI 读完所有附件，不影响其他会话结果。

## 平台特别字段

在字段所属对象下使用 `extensions.<platform>`，例如 `message.extensions.qq.message_seq`。跨平台共有语义应放标准字段；下载、解码参数留在本地扩展中。扩展不能覆盖标准身份、来源或用户指令。

`context_notes: [{kind,text}]` 是已确认的简短事实入口方向，用于确实影响待办判断的平台特殊属性；当前投影不自动读取这个字段。接入前须明确允许哪些事实类型、长度与来源，禁止将扩展 JSON 整包拼入提示词。

## 幂等、版本与迁移

1. 消息身份为平台＋账号＋会话 ID＋原消息 ID；内置实现规范化后计算 SHA-256。正文相同不等于同一消息。
2. 相同身份的内容与命中规则集合都未变化时不再发布；变化产生更高 `revision`。身份资料的 `observed_at/source` 不单独触发内容修订，实际名称变化可以产生新修订。
3. 去重记录、outbox 和正常窗口检查点同事务提交。发布失败保留 outbox，原子改名后重试可恢复。显式重放不推进正常检查点。
4. 处理层以 `event_id` 去重，以 `message_key + revision` 更新；同一已导入事件或已发布文件内容改变会报错。不能宣称端到端“恰好一次”。
5. 旧 v1 批次没有 `identity` 仍可读取，但人物名称显示缺失；重新预览或显式重放才能由提供方补齐，不能直接改写旧 JSONL。
6. 新增可选字段保持 v1 兼容；改变身份或事件语义需要新主版本与迁移器。当前不自动接受所谓 v2 批次。消息稳定 ID 不能因补昵称、换目录或展示方式变化而重建。

当前验证器检查核心身份、时间、修订与数组类型，尚未对所有扩展字段执行 JSON Schema 校验。提供方仍须满足本文契约；开发验收见 [收集层开发指南](collector-development.md)。
