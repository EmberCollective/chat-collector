# 微信 / QQ 数据收集层

主项目根目录：`D:\opt\chat-collector`。采集核心、配置、数据及可选调试工具属于同一个项目。

筛选使用独立 `.sql` 规则集：扫描 → 各规则查询同一份数据 → SQL 合并去重 → 附件处理与增量输出。关键词只匹配当条正文；上下文以 @所有人或文件为中心带出前后各 5 条，详见 [筛选说明](FILTERS.md)。主项目已初始化 Git，本机数据、凭据和运行日志不进入版本控制。

```text
chat-collector/
  collector/             数据库刷新、SQL 筛选、附件归档和增量输出
  config/                微信与 QQ 的统一结构配置
  rulesets/              可注册的只读 SQL 查询
  processor/             可选待办处理核心，使用用户配置的模型
  processing-rules/      用户维护的 Markdown 处理规则
  docs/                  优化方法、开发指南及处理层说明
  data/                  解密快照、状态库、消息批次和附件
  secrets/               本机凭据，不进入版本库
  runtime-deps/          本机 SQLCipher 运行库
  tools/
    qq-recover.cmd        管理员取钥工具
    console/              可选调试面板和 OpenAPI
      start.cmd           调试台启动入口
  tests/                  核心测试
  run.cmd                 核心命令行入口
```

核心可直接使用 `run.cmd`，无需启动调试台或安装 Web 依赖。需要调试时运行 `tools\console\start.cmd`，访问 `http://127.0.0.1:10400`。调试台是本项目的下属工具，它读取根目录配置和数据，没有独立业务数据副本。工具说明与风险见 [调试工具 README](tools/console/README.md)。

迁移记录：原工作区目录保留为指向本目录的 Windows 目录联接，用于兼容历史批次中的绝对附件路径；原实体目录和旧独立调试台已备份。不要将旧兼容路径当成第二套数据删除或另行采集。以后跨机器迁移需同时处理历史绝对路径引用。原微信上游项目、客户端数据和外部媒体路径保持原位。

本地数据库 → 默认时间条件及其他 SQL 条件 → 统一 JSONL 数据 → 可选处理层。采集与筛选没有 AI；处理层独立使用用户配置的 OpenAI 兼容接口，按本地 Markdown 规则输出带来源的 P0–P4 待办。网页和 OpenAPI 仅由可选工具提供。

微信模块从现有项目提取离线解密、WAL 恢复和文本解码核心，独立运行，不依赖原项目的后端、前端或有期限的 native core。QQ 使用官方 SQLCipher 引擎和已有 QQBackup 消息解析器。原项目、原始聊天库和已有导出不删除。

## 启动

当前机器使用 Python 3.13；QQ 的 Windows SQLCipher wheel 已放在 `runtime-deps`，`run.cmd` 会加载它。

QQ 首次取钥：保持 QQ 登录，**右键 `tools/qq-recover.cmd` → 以管理员身份运行**。这台机器已实际验证管理员方式成功；普通权限无法读取 QQ 进程。凭据保存在 `secrets/account-key.json`，不要发给他人。取钥完成后的日常数据库采集不需要读取 QQ 进程。

```powershell
# 单次采集微信
.\run.cmd once --config config/wechat.toml

# 持续运行两个模块，各自使用配置里的扫描间隔
.\run.cmd run --config config/wechat.toml --config config/qq.toml

# 用 Unix 秒重放指定时间段；不移动正常定时采集的检查点
.\run.cmd once --config config/wechat.toml --start 1794326400 --end 1794412800
```

按 Ctrl+C 停止前台进程。QQ 必须先取得并验证该账号的本地数据库密钥；缺失密钥会报告失败，不会伪造空结果或推进该模块的时间检查点。其他模块可继续运行。

本机后台进程信息见 `data/collector-process.json`，日志位于 `data/collector.log` 与 `data/collector-errors.log`。停止后台任务可在任务管理器中确认对应 PID 和命令行后结束该采集进程，不要结束其他 Python 进程。每轮结束后等待配置的间隔，不保证整点触发；关闭终端后继续运行，电脑关机后需重新执行上面的持续运行命令。未安装开机自启动项。

迁移机器时安装 `requirements.txt`，重新设置两个配置文件的路径；本地 `runtime-deps` 只适用于 Windows x64 / Python 3.13。不要把私有密钥放进 Git 或输出 JSONL。

## 每模块一份、相同结构的配置

`config/wechat.toml` 与 `config/qq.toml` 使用相同分区。所有相对路径相对于配置文件所在目录解析。

| 分区 | 配置 | 含义 |
|---|---|---|
| 顶层 | `platform` | `wechat` 或 `qq` |
| `source` | `account_id` | 当前账号，参与消息身份和状态隔离 |
| `source` | `encrypted_dir` | 本机原始加密库目录，只读取 |
| `source` | `decrypted_dir` | 本模块生成的解密快照目录 |
| `source` | `work_dir` | QQ 临时工作目录，可放 SSD；正式快照仍按 decrypted_dir 保存 |
| `source` | `key_file` | 本地密钥文件路径，配置中不存明文密钥 |
| `source` | `refresh` | 是否先刷新解密快照；已有明文库可设 `false` |
| `storage` | `state_file` | 检查点、去重状态和待输出数据 SQLite |
| `storage` | `output_dir` | 提供给后续处理层的 JSONL 批次目录 |
| `schedule` | `interval_seconds` | 两次采集间隔，默认 60 秒 |
| `schedule` | `initial_lookback_seconds` | 首轮回溯长度，默认 24 小时 |
| `filters.rulesets` | `file` / `enabled` / `params` / `recent_count` | 注册 SQL 文件、参数及上下文条数；结果取并集去重 |

筛选只有 SQL 规则集一个入口。每项通过 `enabled=true/false` 独立启停，省略表示启用。空注册表或全部关闭时不采集、不推进时间记录。三个独立规则集的粘贴示例见 [three-rulesets.json](examples/three-rulesets.json)。群聊、关键词、文件、@所有人均写在 SQL 中；不存在另外一组基础条件。默认注册 `all.sql`，选取时间窗内全部消息。

例如，收集指定群中包含关键词、文件或 @所有人任一条件的消息：

```toml
[[filters.rulesets]]
file = "../rulesets/group-content.sql"
params = { group_id = "123456@chatroom", keyword = "截止" }
```

这个文件内部使用“群 ID AND（关键词 OR 文件 OR @所有人）”。多个注册文件的结果取并集，因此要限制每条规则的群范围，应写入各自 SQL；不能把群规则和关键词规则分别注册后期待它们自动取交集。启用该示例时替换默认 all.sql 注册项。

群标识来自数据库：微信通常为 `...@chatroom`，QQ群为群号字符串；不要填显示名称。

## 时间条件就是默认 SQL 条件

首轮：`[当前时间 - initial_lookback_seconds, 当前时间)`。

以后：`[上次成功轮次的结束时间, 本轮开始时的当前时间)`。

微信使用原表 `create_time`；QQ 使用原表 `40050`。查询采用 `>= 起点 AND < 终点`，边界不重叠。仅在生成的明文快照中建立时间索引；原始数据库不添加索引。文件变化缓存只用于跳过未变化的解密文件，不是另一套消息增量算法。

时间戳使用 Unix 秒，`collected_at` 使用带时区的 UTC ISO 时间。启动时不额外按消息流水号补抓、不隐式扩大时间窗。较早时间戳的消息如果后来才同步入库，以及时间窗外的旧消息修改，需要显式重放相应时间段。修改过滤条件默认只影响后续轮次；需要回溯时同样使用重放命令。

## SQL 规则集

每个 `rulesets/*.sql` 是一条只读查询，返回 `identity` 一列。规则注册和公共字段约定见 [FILTERS.md](FILTERS.md)。各规则完整查询共同输入，通过 SQL 主键合并命中结果后才处理附件；查询禁止写入、附加数据库和加载扩展。

已提供 `conversation.sql`、`keyword.sql`、`files.sql`、`mentions-all.sql`、`group-content.sql` 和 `context.sql` 等示例。组合逻辑直接使用 SQL 的 AND、OR、EXISTS、JOIN 和窗口函数；值通过各注册项的 params 绑定。

旧 `filters.keywords / conversation_ids / files / mentions_all / match / sql_file / code_file / rules` 已移除。旧字段会明确报错，防止旧限制被静默忽略；将它们的条件写入完整 SQL 后，仅保留 filters.rulesets。当前本机旧条件均未启用，配置已备份并清理。

## 输出和明确的去重规则

每行是一个 `schema_version = 1` 事件：

```json
{
  "schema_version": 1,
  "event_id": "唯一输出事件哈希",
  "message_key": "消息身份哈希",
  "revision": 1,
  "operation": "upsert",
  "payload_sha256": "内容哈希",
  "matched_rules": [{"file": "../rulesets/files.sql", "params": {}, "recent_count": 0, "sql_sha256": "SQL文本哈希"}],
  "collected_at": "2026-11-12T04:00:00+00:00",
  "window": {"start_inclusive": 1794446400, "end_exclusive": 1794446460},
  "message": {
    "platform": "wechat",
    "account_id": "账号",
    "conversation_id": "群或会话ID",
    "conversation_type": "group",
    "message_id": "原平台消息ID",
    "sender_id": "发送者ID",
    "sent_at": 1794446412,
    "kind": "file",
    "text": "报告.pdf",
    "mentions_all": false,
    "attachments": [{"type": "file", "name": "报告.pdf", "size_bytes": 1234}],
    "source": {"database": "message_0.db", "table": "Msg_...", "local_id": 42}
  }
}
```

1. **身份去重**：平台 + 账号 + 会话 ID + 原始消息 ID，规范化后计算 SHA-256。微信优先使用服务端 ID；没有时使用数据库、表和本地 ID。不同群、不同账号、QQ 与微信之间不因文字相同而删除消息。
2. **内容比较**：同一身份的标准化内容计算 SHA-256；读取路径等 `source` 定位信息不参与内容修订判断。消息内容和命中规则集合都与上次一致才不输出；任一变化产生新 `revision` 的 `upsert`。顶层 `matched_rules` 保存文件、参数、上下文条数和 SQL 摘要，单独计算规则集合哈希，不改变消息内容哈希；规则顺序变化不产生修订。内容改动后又恢复，也产生新的修订号。
3. **批次重试**：消息状态、输出队列、成功时间检查点在同一个 SQLite 事务提交。中途失败整轮回滚。输出先写临时文件再原子改名；重启先补完未导出的队列，同一序号段复用同一文件名并检查内容。
4. **下游幂等**：后续处理层按 `event_id` 去重；按 `message_key` 和递增 `revision` 更新材料。消费者可能重读文件，因此不宣称端到端恰好一次交付。

上下文规则可显式输出触发点之前的有限历史消息，时间早于事件 window 起点不代表扫描进度倒退；后文逐轮匹配，第 6 条不再带出。完整四项注册示例见 [collection-rulesets.json](examples/collection-rulesets.json)。

相同内容跨群转发、跨平台重复材料的语义合并属于后续处理层；此处保留来源证据。不会根据文字推断任务负责人、截止时间、优先级或提醒计划，也不会把删除记录自动解释成撤回事件。

`data/batches/*.jsonl` 是可消费的完整批次。忽略 `.tmp` 文件。每批最多 1000 条；空轮次只报告计数，不产生空文件。附件在 SQL 筛选后归档到 `data/assets`，详情见 [照片与文件保存说明](../照片与文件保存说明.md)。附件后续补齐会在显式重放时产生新的消息修订。

`[attachments]` 统一配置 `enabled`、`archive_dir`、`allowed_roots`。`source.media_dir` 指向平台媒体目录；微信另外配置 `resource_db`、`image_cache_dir`、`decoder_dll`。本机微信媒体在 D 盘，不能改回只有数据库的 C 盘同名目录。微信资源索引和 hardlink 库随消息库刷新，防止新附件一直查询旧索引。QQ 已允许读取 D 盘下载目录中的数据库指定文件。

QQ `@所有人` 使用消息元素的 protobuf 字段 `45102 = 1`，不依赖显示文字；`0` 是普通文本、`2` 是单人提及。本机最近 20 万条群消息抽样中，101 个全员标记与 20 个同名普通文本、1 个同名单人提及可区分，统计见上级目录 `qq-mention-evidence.json`。未知消息类型保留原始正文的 Base64，供后续确定性解析。

QQ 完整刷新由官方 SQLCipher 解密稳定 DB/WAL 副本，逐表复制原列和 rowid、核对行数，重建原索引后检查完整性，再原子替换快照。本机 `sqlcipher_export` 整库辅助函数报 malformed，但逐表原生 SQL 读取与索引重建均成功；没有自制加密引擎，也没有丢弃报错表。当前消息采集范围为群聊和私聊主表；临时会话等其他表保留在明文数据库中，尚未纳入标准消息输出。

## 本地验证与来源

```powershell
python -m pytest tests -q
```

包含时间边界、压缩消息、SQL 筛选、文件/@所有人标记、去重、内容修订、失败回滚、输出重试、原生图片接口、附件校验和错误配置测试。实际数据库检查、重放去重和归档哈希结果见上级目录 `collector-verification.json`。

微信原算法来源及保留范围见 `collector/vendor/wechat/UPSTREAM.md`；QQ 组件保留其上游许可。微信原 checkout 没有根 LICENSE，本交付用于你的本地项目，不据此声称具有对外再分发许可。

## 扫描间隔与抓取解码强度

两平台独立使用相同配置。面板模块卡片的“扫描与强度”打开对应配置；CLI 读取同一份 TOML，无需控制台服务。

```toml
[schedule]
interval_seconds = 60          # 每轮完成后的等待秒数
refresh_interval_seconds = 0   # 自上次成功采集窗口终点起的最小刷新间隔；0 为每轮
initial_lookback_seconds = 86400

[processing]
intensity = "full"             # metadata / standard / full
```

刷新间隔由用户单独配置，不由强度档位决定。实际间隔和强度以各模块配置为准；未指定新字段的旧配置采用每轮刷新、完整档。面板扫描间隔支持 5–86400 秒；CLI 支持正整数。刷新间隔支持非负整数，单位为秒。

例如扫描间隔 30 秒、刷新间隔 300 秒，后台每轮结束后等待 30 秒检查；未到刷新时间时不读取旧快照推进断点，也不处理附件。到期后的下一次检查才刷新，实际刷新间隔可能更长。首次采集立即执行；失败不推进断点。手动“执行一轮”遵守刷新间隔，指定时间段的重放绕过间隔、按 source.refresh 决定是否刷新。关闭 source.refresh 后，使用用户管理的明文快照，刷新间隔不生效。

| 强度 | 消息 | 附件与图片 |
|---|---|---|
| 轻量 metadata | 全部匹配消息及原附件元数据 | 不查找、复制、解密媒体文件，附件标记 deferred |
| 标准 standard | 全部匹配消息 | 归档普通本地文件、QQ 本地媒体；微信保留 DAT 密文并复用可读缓存，不做新 DAT 解密或 WXGF 转换 |
| 完整 full | 全部匹配消息 | 在标准基础上尝试微信 DAT 解密与 WXGF 转换，保留原密文变体 |

attachments.enabled 是保存附件的总开关；关闭时三档均不归档、解码。QQ 本地图片通常已可读，标准与完整使用同一路径，不声称完整档会额外恢复云端图片。预览始终只读，不处理附件。

风险：强度不是 CPU/内存硬限额，较大数据库的刷新仍可能占用磁盘；低档会推迟或放弃当前轮附件保存，原缓存被客户端清理后可能无法补齐。配置改变只影响后续执行，不自动回补历史；需要用完整档重放指定时间段。低档重放已归档消息可能产生仅元数据或较低质量的最新修订，原历史事件和归档文件不删除，完整档重放可在源仍存在时补齐。时间筛选仍是 [上次成功终点, 本轮开始)，不添加 ID 游标、重叠窗口或消息截断。


## 处理层与开发文档

处理层是同一项目下独立的 `processor/` 包，消费采集批次。规则位于 `processing-rules/default.md`，模型、上下文及输出路径位于 `config/processor.toml`。未配置模型时可以预览上下文，正式处理明确提示缺少配置，不会生成假待办。调试工具只是额外入口。

- [数据契约](docs/data-contract.md)：消息、人物、附件、来源及兼容边界。
- [收集层开发指南](docs/collector-development.md)：提供方适配、SQL、姓名解析和验收。
- [提供方接入](docs/provider-api.md)：发布标准批次、注册按需附件工具和外部平台示例。
- [AI 输入结构](docs/ai-context.md)：实际输入字段、短引用和合成示例。
- [开发指南](docs/development.md)：模块边界、扩展入口和验证方法。
- [性能优化](docs/optimization.md)：QQ 分段计时、SSD 临时目录、附件查找与安全发布。
- [微信性能](docs/wechat-performance.md)：批量索引事务、有限诊断样本和数据对照。
- [处理层说明](docs/processing.md)：Markdown 规则、上下文、结构化输出和去重。
- [处理调试台](docs/console-processing.md)：配置、规则编辑、预览与待办检查。

QQ 的 SSD 临时目录会暂存明文聊天库，正常结束自动清理，强制终止可能留下文件；请使用本机私有目录。正式库写回机械盘仍需时间，此优化不是页级增量解密。微信保留全部页检查，仅限制重复的详细诊断计算。

处理层配置远程模型后，会把所选消息及来源字段发送给该服务；附件初始只有索引；AI 请求附件工具后，选中的正文或视觉图块才会发送给配置的模型。模型结果可能误判，未知内容应留空并标记复核，保留原始证据供检查。AI 循环、工具、压缩与重试已改用官方 Pi 原生 SDK，见 [Pi 运行时](docs/pi-runtime.md)。处理层不执行聊天中的指令，也不自动向联系人或群聊发送消息。


当前 SQL 注册表为 @所有人、文件、前后各五条上下文，不再启用 all.sql。处理起点在 config/processor.toml 的 input.start_at，首次不全量处理历史。单会话字符上限为 context.max_conversation_chars；自动合批或强制单会话由 agent.batching 配置。完整关联片段超限时明确报错，不静默截断。`.env` 已支持，且排除在 Git 之外。
