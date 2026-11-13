# 消息渠道开发指南

渠道接收消息层整理好的待办包，负责映射外部平台、发送材料、返回回执，以及接收用户操作。渠道不调用模型、不读取聊天数据库、不修改采集或 SQL 规则，也不直接写处理层数据库。

已有账号的配置见 [渠道配置](messaging-channels.md)；输入字段见 [消息层契约](messaging.md)；自动调度见 [自动运行](automation.md)。本指南说明如何接入一个新渠道。

## 选择接入路径

| 场景 | 路径 | 需要改什么 |
|---|---|---|
| 已有 n8n、MCP/OAuth 工作流或外部服务 | 注册 `kind="webhook"` | 目标配置和接收端；项目核心不用改 |
| 需要平台 SDK、文件上传、原生卡片或专用回调 | 项目内 Python 适配器 | 适配器、显式分发、配置管理与测试 |

`target.id` 是同一渠道下某个账号/群/资料库实例的稳定标识；`kind` 是发送实现。一个实现可以配置多个目标，投递、回执和回调都必须隔离。当前没有动态加载 `.py` 的插件注册系统，不能只放入文件就认为已接入。

## 优先复用 Webhook

在消息层配置中添加一个关闭的目标，凭据只保存环境变量名称：

```toml
[[targets]]
id = "team-workflow"
kind = "webhook"
enabled = false
url_env = "CHAT_TEAM_WEBHOOK_URL"
token_env = "CHAT_TEAM_WEBHOOK_TOKEN"
mode = "sync"
timeout_seconds = 30
```

地址和 token 放本机 `.env`，不要提交 Git。全局 `enabled` 和目标 `enabled` 同时开启才会发送。先用隔离配置与合成数据验证，再启用正式目标；不能直接运行使用真实数据的 `once` 作为无副作用检查。

| `mode` | 收到的任务投递 |
|---|---|
| `sync` | 新建、内容/材料变化、状态变化；适合资料库和日历 |
| `notify`（默认） | 变更投递，加上 P0–P4 配置允许的周期提醒 |

Webhook 的请求、回执示例见 [外部工作流契约](messaging-channels.md#外部工作流契约)。接收端应完成以下步骤：

1. 验证 Bearer token 和请求结构，按 `delivery_id` 查持久去重记录；并发重复请求也只能执行一次。
2. 按目标实例和 `task_id` 查远程对象映射。`upsert` 更新已有对象；`reminder` 发送提醒，不重复创建页面或日程。
3. 上传所需材料，执行远程操作，保存对象映射与结果。若进程可能在远端成功后、本地保存前退出，使用平台幂等键或查询对账恢复。
4. 实际操作及回执持久化完成后，返回 `accepted=true`、匹配的 `delivery_id`，以及可选 `remote_id`、`url`。重复请求返回原回执。

发送器带 `Idempotency-Key: <delivery_id>`，但该请求头本身不能保证接收端幂等。仅接收进队列不能冒充最终成功；现有发送器不支持异步查询某个 Webhook 作业完成状态。HTTP 200/202 如果没有规定的最终回执，会进入 `unknown`。

## 项目内适配器接口

以 `messaging/feishu.py` 或 `messaging/channels.py` 中的 Notion 实现为参照，复用成熟 SDK。独立模块按下面的函数签名实现，并由 `channels.deliver` 显式调用：

```python
def deliver_example(target, envelope, config, previous=None):
    """返回 JSON 可序列化回执，或抛出约定的 DeliveryError。"""
```

| 参数 | 含义与约束 |
|---|---|
| `target` | 本地配置的目标实例；凭据用 `_secret(target, "token_env", config)` 读取 |
| `envelope.delivery_id` | 本次投递的稳定 ID；重试时保持不变 |
| `envelope.kind` | 常规任务为 `upsert` / `reminder`；`alert` 是自动化故障通知专用路径 |
| `envelope.task` | 统一行动包，含 `task_id`、`revision`、步骤、时间、参与人员、材料和精简来源 |
| `config` | 消息层配置、状态和归档位置，不是模型配置 |
| `previous` | 同目标配置、同任务最近一次成功投递的回执，首次可为 `None` |

`people` 表示参与人员；不能把所有参与者映射为负责人。`notes` / 兼容字段 `review_reasons` 是说明，不设置人工审核门槛。状态包括 `open`、`completed` / 兼容 `done`、`cancelled`、`superseded`。完成或取消不删除本地材料。

成功回执建议采用：

```json
{
  "delivery_id": "与输入一致",
  "remote_id": "平台消息或页面ID",
  "url": "https://example.com/tasks/remote-id"
}
```

核心当前只验证返回值是可序列化字典；适配器必须自行确认平台业务成功、必要 ID 存在，不能把 HTTP 成功直接当作业务成功。回执只保存恢复所需字段，不保存 token、签名地址或完整平台响应。

远程 JSON 使用 `public_envelope(envelope)` 的返回值；不能直接序列化内部 `envelope`。内部包可能含本机文件路径，白名单输出会移除它们。平台正文从可信字段投影后按平台规则转义，禁止聊天正文制造 @全体、按钮或伪链接。资源哈希可用于本地完整性校验，不必显示在卡片上。

### 注册改动位置

| 文件/位置 | 新适配器要做的事 |
|---|---|
| `messaging/<渠道名>.py` | 发送、SDK 业务结果校验、错误分类，必要的独立持久状态 |
| `messaging/channels.py::deliver` | 增加 `kind` 显式分发；保留全局/目标开关和输入校验 |
| `messaging/config.py::load_config` | 若新字段需要校验，在此覆盖直接读取 TOML 的入口；当前只做通用目标校验，并不拒绝所有未知 kind |
| `messaging/management.py` | 扩展 `FIELDS`、`_validate_target` 的必填字段及类型校验；保证面板只返回凭据变量名 |
| `config/messaging.toml` | 添加关闭的示例目标；用独立测试配置验证，不改现有目标身份 |
| `tools/console/static/automation.js`（可选） | 目标字段从管理接口 schema 生成；可补渠道/字段中文名称，无需新建独立开关 |
| `messaging/management.py::send_test` | 检查合成卡片与材料是否适用；如新增按钮，必须有隔离的测试回调处理 |
| `tests/`、本指南及渠道配置文档 | 补失败路径、去重、材料和配置测试，说明实际能力 |

发送入口、回调监听和故障报警是不同能力。当前统一自动化的 `listener` 专用于飞书，报警目标也只支持 `feishu` / `feishu_app`；添加 `kind` 不会自动注册其他平台的监听器或报警器，需要时单独接入生命周期和配置检查。

不要在现有 `feedback.db` 添加其他平台专用表：当前该库只允许反馈队列和 `feishu_*` 表。需要上传缓存、对象映射或回调 token 时，使用该适配器独立状态库，核对库身份并隔离目标实例，不复用处理层数据库。

## 失败、重试与去重

调度、提醒间隔和次数由核心管理。SDK 若会自动重试写请求，应关闭，或明确证明其幂等语义；不要在适配器内叠加无限重试或自行启动定时循环。

| 结果 | 适配器行为 | 核心结果 |
|---|---|---|
| 平台确认全部要求已完成 | 返回回执 | `sent` |
| 确定未执行/明确拒绝 | `raise DeliveryRejected("稳定错误码")` | `retry`，耗尽后 `failed` |
| 超时、响应丢失、无法判定是否已执行 | `raise DeliveryUncertain("稳定错误码", receipt=部分回执)` | `unknown`，暂停该任务在该目标的后续投递 |
| 未分类异常或发送期间进程中断 | 不伪造成功 | 保守进入 `unknown` |

`schedule.max_attempts` 包含首次尝试；退避为 `min(retry_base_seconds × 2^(attempts-1), 86400)` 秒，实际重试还取决于下一轮调度。当前明确拒绝也会消耗配置的有限重试预算，没有单独的“永久错误立即终止”类别。通用 HTTP 实现将 5xx 视为未知结果，避免服务已写入却返回错误时重复发送。

`previous` **不是本次失败的部分回执**。如果“卡片成功，文件失败”，需要像飞书实现一样自行持久保存各步骤结果；重试只补缺失文件。部分回执可通过异常的 `receipt` 留给核心展示，但不要假定下次会通过 `previous` 返回。成功文件不能随着提醒反复上传或发送。

`delivery_id` 由目标配置、任务修订、本地状态版本、材料内容、投递类型和提醒锚点等生成。重试保持 ID；修订、目标配置或提醒轮次变化可能生成新 ID。远程对象应另以 `(目标实例, task_id)` 维护稳定映射。系统不承诺跨 SQLite 和远程平台的“恰好一次”。

未知结果先核对远端，再通过 `resolve_delivery` 或面板明确重试/确认送达。新适配器若有自己的 `sending/unknown` 状态，需要接入 `messaging.core.resolve_delivery` 的恢复路径，否则核心重试可能仍被适配器锁住。现有函数对飞书、Notion有专项处理，不能默认适用于所有平台。

投递重试与自动化作业重试是两层预算；作业重新运行不会释放 `unknown`。故障报警来自独立 `automation` 服务及其配置，单独调用 `channels.deliver` 不会自动报警。

## 附件与成果

通过 `messaging.package.material_path(package, resource_id, config)` 获取经路径范围和哈希核验的材料，返回 `(Path, 文件名)`。不要直接打开回调传来的路径，也不要把 `D:\...` 当作可下载链接。

资源 `role=input` 是执行材料，`role=result` 是用户上传的完成成果。适配器按平台容量上传，保存文件 ID、消息 ID及结果；限制超标或原件缺失要明确报告，不能伪称已发送。需要的文件未全部发送成功时，应保留已成功步骤并返回失败/未知结果。

外部 Webhook 只收到材料索引和允许的 HTTP(S) 链接，不收到本机路径或文件 Base64。远程工作流需配置受控文件桥接或上传到平台/对象存储。本机面板的材料接口有本机来源和令牌校验，不是现成的公网文件服务器。

## 回调更新待办

回调适配器先使用平台 SDK 验签/校验连接，核对应用、目标群、操作者权限、消息与当前卡片代次，再从服务端保存的 token 映射解析任务和动作。不能相信外部请求自报的 `task_id`、`actor` 或 `expected_revision`。

完成鉴权后调用现有队列：

```python
from messaging.feedback import enqueue_feedback

# 参数均来自已校验事件及服务端映射。
ack = enqueue_feedback(
    config, target_id, event_id, task_id, "complete",
    {"platform": "example", "user_id": verified_user_id, "source": "card_callback"},
    expected_revision=card_revision,
)
```

该函数验证结构、目标注册与事件去重，**不代替平台鉴权或操作者授权**。动作支持 `complete`、`reopen`、`cancel`、`snooze`。平台重复投递必须复用同一个稳定 `event_id`；同一按钮双击若代表同一次操作，也应使用同一操作标识。`queued` 只表示已持久入队，不能立即提示正式待办已完成。

独立反馈任务调用 `process_pending(config)`，通过处理层公开 API 回写，成功成为 `applied`；修订冲突不覆盖新状态。详见 [状态 API](task-status-api.md) 和 [反馈队列](messaging.md#操作反馈与状态回写)。此过程不调用模型。新平台的外部回调 HTTP 服务需要自行提供鉴权入口，项目没有通用匿名回写端点。

## 本地验证与上线

用临时目录、关闭的目标和合成待办进行开发；网络响应用 `httpx.MockTransport` 或 SDK 模拟，不使用真实聊天作为测试数据。

| 验证 | 最低要求 |
|---|---|
| 发送与修订 | 首次创建、相同投递重放、修订更新已有对象、提醒不误建资料页 |
| 失败 | 明确拒绝、连接失败、读超时、无效回执、进程中断；不得无限重试 |
| 材料 | 成功、缺失、越界、哈希不符、过大、部分成功后仅补失败部分 |
| 回调（若支持） | 伪造 token、错误群/账号、无权限人员、旧修订、双击及重复事件 |
| 配置与隐私 | 双开关、多个目标隔离、凭据不出现在回执/日志/面板、合成测试不修改正式待办 |

已有测试参考：`tests/test_messaging_channels.py`（Webhook/Notion）、`test_feishu.py`（卡片与附件）、`test_messaging_core.py`（调度去重）、`test_messaging_management.py`（配置与测试发送）、`test_feedback_end_to_end.py`（状态回写）。项目根目录可运行：

```powershell
python -m pytest tests/test_messaging_channels.py tests/test_messaging_core.py tests/test_messaging_management.py tests/test_feedback_end_to_end.py -q
```

本机使用 `runtime-deps` 安装可选依赖时，运行前设置 `$env:PYTHONPATH="$PWD/runtime-deps"`。新增平台只运行上述测试不代表已验证，必须加入该平台测试。

上线前在专用目标发送一次明确的合成测试，核对远端对象、文件和真实回调。重启持有配置或已导入适配器的后台服务，再启用正式目标；不要重复启动第二套调度器。未验证的 OAuth、文件上限、客户端显示和平台权限应在该渠道文档中明确标注。
