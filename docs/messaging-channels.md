# 飞书、Notion 与 Notion Calendar 接入

消息层投递通过数据契约校验的待办包，不调用模型，也不设置人工复核门槛。缺失信息作为 notes“说明”随任务展示。正式发送须同时启用 `config/messaging.toml` 的全局 `enabled` 与对应目标；启用状态以本机配置为准，预览和本地同步不要求凭据。接入新平台见 [渠道开发指南](channel-development.md)。

安装可选渠道依赖：`python -m pip install -r messaging/requirements.txt`。Notion 使用社区维护的 `notion-client==3.1.0`，固定 API 版本 `2025-09-03`；不是自行实现 SDK。官方维护的 JavaScript SDK 是 `@notionhq/client`。

## 三种接入方式

| 目标 | 当前路径 | 附件与限制 |
|---|---|---|
| 飞书应用机器人 | 官方 `lark-oapi==1.7.5`，卡片与 WebSocket 回调 | 卡片完成/恢复/稍后按钮；文件直接发送到同群；失败文件有限重试，unknown 停止重发 |
| 飞书提醒 | 自定义机器人 Webhook，支持签名 | 正文提供动作、期限、人物、材料和来源；Webhook 无文件上传能力，本地文件明确标注未上传，不伪装成可点击地址 |
| Notion 待办资料库 | 官方 Data API，经 Python SDK 更新数据源页面 | 已验证归档上传为 Notion 托管文件，正文链接可点击；单文件最大 20 MiB，工作区自身配额仍适用 |
| Notion Calendar | 数据库日期展示，或 `mode="sync"` Webhook 接已有 OAuth MCP 工作流 | 前者显示 Notion 待办日期；后者创建真实日历事件，需要授权后核验 Calendar 工具参数 |
| 其他信息模块 | 统一 Webhook，可接 n8n 或已有服务 | 接收统一契约，自己映射目标字段；可用本地受控接口按材料 ID 取文件，不接收任意磁盘路径 |

## 飞书

当前主接入方式是 `kind="feishu_app"` 应用机器人。完整创建步骤、稳定的应用权限 JSON、群 ID/操作者配置、回调长连接和 SDK 命令见 [飞书应用配置](feishu-setup.md)。待办与故障通知可共用专用群；普通消息与回调队列分别执行。文件的部分成功回执会持久保存，失败不会被当作整体成功。

下面保留 `kind="feishu"` 的简易 Webhook 兼容方式；它没有卡片反馈和文件上传能力。

在目标群里创建自定义机器人，取得 Webhook 地址；若启用签名，也保存签名密钥。项目 `.env`：

```dotenv
CHAT_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/实际值
CHAT_FEISHU_WEBHOOK_SECRET=实际签名密钥
```

凭据环境变量优先于 `.env`，不得提交 Git。提醒频率来自消息层 P0–P4 配置。单次正文超过飞书限制会明确失败，不能静默截掉执行步骤。机器人拒绝会进入退避；连接后超时或服务器错误属于结果未知，不能盲目重发。

Webhook 机器人不能直接上传 PDF、DOCX 或图片。要让附件在飞书内打开，优先使用本项目已实现的 `feishu_app`；也可把统一包交给已有 n8n/飞书应用工作流。系统不自动建立公网文件服务器，也不把 `D:\...` 本地地址作为网络链接。

## Notion 待办资料库

创建内部集成，将目标数据库共享给该集成，并配置 `.env` 的 `CHAT_NOTION_TOKEN`。`data_source_id` 是数据库中的**数据源 ID**，不能随意用数据库容器 ID 替代；可通过 Retrieve database 返回的 `data_sources` 查明。

在已有数据源中建立以下属性，名称可以在 `[targets.properties]` 映射：

| 配置键 | 默认属性名 | Notion 类型 | 内容 |
|---|---|---|---|
| `title` | Name | title | 用户要完成的动作 |
| `task_id` | Task ID | rich_text | 稳定本地待办 ID，用于查询与更新 |
| `priority` | Priority | select | P0–P4 |
| `status` | Status | select | open、completed、cancelled、superseded |
| `date` | Date | date | 优先截止时间，其次开始时间；未知则为空 |
| `instructions` | Instructions | rich_text | 怎么做、相关人员、地点、材料、来源、缺口 |
| `files` | Materials | files | 本轮已上传的材料 |

若 Status 使用 Notion 的原生 `status` 属性，在该目标设置 `status_type="status"`，并事先建立对应选项。程序不改写数据源结构。

先分页查询 Task ID，再更新已有页面；出现多个相同 Task ID 时停止，避免猜测覆盖。收据保存页面 ID 和文件上传 ID，修订复用它们。已知页面被手动删除、归档或改了 Task ID 时停止同步，由用户检查，避免再造一份任务。

只管理表中映射的属性，**不修改页面正文与手写笔记**；也不反复追加页面块。请不要把需要长期手动维护的附件放入 Materials 这个受管理属性；可放正文或另建属性。Instructions 按 Notion 每段 2000 字符拆分，超过 API 容量明确报错，不截断。

上传前再次校验材料位于消息层归档内且 SHA-256 一致。上传未完成不返回成功收据。首版采用官方单段上传；超过 20 MiB 明确拒绝该次投递，可改用外部工作流的分段上传/对象存储。未挂接成功的 Notion 临时上传会按官方约定过期；已挂接材料保留在 Notion。完成待办只更新状态，不删除材料。

## Notion Calendar：已核对的两条路径

### 展示待办日期

Notion 目标写入 Date 属性后，用户在 Notion Calendar 中连接该数据库及日期字段，即可查看带日期的任务。无日期的事项仍留在资料库，不虚构日程。建议在对应数据库视图中过滤 `Status=open`，避免已完成/取消事项继续显示为活跃安排。

这条路径是 Notion 数据库页面的日期展示，不冒充一个独立日历事件。也不承诺 Notion 会按本地 P0–P4 频率自动推送；频率由消息层提醒渠道执行。

### 真实日历事件：官方 MCP 工作流

2026-11-13 查阅的官方文档已列出以下工具：

- `notion-calendar-list-calendars`、`notion-calendar-list-events`
- `notion-calendar-create-events`、`notion-calendar-update-events`、`notion-calendar-cancel-events`

这些工具仅对**用户授权的 OAuth MCP 连接**开放；workspace-owned MCP token 不支持 Calendar。Notion 普通 Data API token 不能直接替代日历用户授权。

官方托管 MCP 地址是 `https://mcp.notion.com/mcp`，采用 Streamable HTTP。可复用已有支持 OAuth 的 MCP 客户端或 n8n 工作流；不自写 OAuth。官方文档也给出了 `mcp-remote`、Pi 的 `pi-mcp-adapter` 接入路径。本项目的处理 Pi 不因此获得发送权限；消息工作流仍与提取层分离。

配置模板的 `notion-calendar` 目标使用 `kind="webhook"`、`mode="sync"`。接收工作流授权后必须先获取实际工具 schema 和日历 ID，再映射统一包；不能猜测参数，也不能每次提醒都调用 create。保存 `task_id -> calendar/eventId`，修订 update，取消/完成按业务选择取消或结束；跨日历、重复事件和参会人通知需由该工作流明确处理。此次无凭据向官方 MCP 发起 initialize，返回 HTTP 401，无法取得用户 Calendar 工具 schema；未连接用户 OAuth，也未创建真实 Calendar 事件。

## 外部工作流契约

请求头 `Idempotency-Key` 是稳定 `delivery_id`。请求体：

```json
{
  "schema_version": 1,
  "delivery_id": "目标与本次投递的稳定ID",
  "kind": "upsert",
  "remote_id": "可选：上次回执的页面或日历事件ID",
  "task": {
    "task_id": "本地待办ID",
    "revision": 2,
    "title": "填写并提交申请表",
    "priority": "P1",
    "status": "open",
    "action_steps": ["填写申请表", "核对后按通知方式提交"],
    "resources": [{"id": "材料ID", "name": "申请表.docx", "type": "file", "status": "ready"}]
  }
}
```

实际 task 还包含 event、time、people、location、review_reasons、精简 sources 等。外发字段逐层经过白名单：time 只含 start/deadline/timezone/text，people 只含 name/role，文本字段和 action_steps 等数组元素不能携带额外对象。不传 `local_path`、采集数据库路径、凭据和原聊天全文。资源链接仅允许无内嵌凭据的 HTTP(S) 地址；文件默认通过 ID 引用，不塞 Base64。资源 role=input 为执行材料，role=result 为完成成果，其 note 保留用户填写的成果说明。

已有 n8n 流程的最小连接方式：Webhook → 按 delivery_id 查询持久去重表 → 按 task_id 查询远程映射 → 上传需要的材料 → 平台新增/更新 → 保存映射与回执 → Respond to Webhook。重复 delivery_id 返回已保存收据，不能再次新增。响应必须等实际操作及回执持久化成功后返回：

```json
{"accepted": true, "delivery_id": "与请求一致", "remote_id": "远程对象ID", "url": "https://目标详情页"}
```

HTTP 200 但缺少匹配回执仍视为结果未知。配置 `token_env` 可让发送器带 Bearer token 验证接收端。`mode="sync"` 只同步变更；默认模式还接收按优先级产生的 reminder。接收方必须把提醒与创建资源分开。文件上传能力、远程状态回流和 OAuth 由接入模块负责；本轮没有安装或伪造一个未配置的 n8n 服务。

## 官方依据与验证边界

- [Notion MCP 概览](https://developers.notion.com/guides/mcp/overview)
- [Notion MCP 连接方式、OAuth、Pi 与文件能力](https://developers.notion.com/guides/mcp/get-started-with-mcp)
- [Notion Calendar MCP 工具与权限](https://developers.notion.com/guides/mcp/tools-calendars-reference)
- [Notion Data API 的数据源迁移](https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03)
- [Notion 官方文件上传流程与限制](https://developers.notion.com/guides/data-apis/uploading-small-files)
- [Notion API 创建页面](https://developers.notion.com/reference/post-page)
- [飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)
- [Python Notion SDK](https://github.com/ramnes/notion-sdk-py)

测试使用真实 SDK 与本地 HTTP 模拟响应，覆盖分页更新、取消、上传成功/失败、附件完整性、凭据不外泄、重复回执和超时。本机飞书目标已验证实际卡片、附件和回调；Notion、Calendar OAuth 及其他未配置目标尚未完成真实联调。新增目标应保持关闭，验证后再启用。
