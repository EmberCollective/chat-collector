# 收集层开发指南

先读 [数据契约](data-contract.md)，再按本页接入。目标是让处理层消费统一事实；不要让模型理解平台数据库结构，也不要把平台适配逻辑写进处理规则。

## 模块分工

```mermaid
flowchart LR
  A[平台原始数据库] -->|只读稳定快照| B[平台适配器]
  B -->|标准消息| C[公共 SQL 查询表]
  C -->|各规则查询并集| D[选中消息]
  D -->|仅查询涉及的身份| E[身份与附件索引]
  E -->|事务及 outbox| F[不可变 JSONL]
  F -->|契约校验与短引用| G[Pi 待办处理]
  G -->|附件编号请求| H[提供方准备与公共读取能力]
  H -->|文本或图片证据| G
```

适配器将原始 SQL 表转换为统一字段；注册 SQL 规则查询统一表。规则通常不需要为 QQ、微信分别重写。平台专有条件可用标准字段或本地 `body` 中的扩展，但相关语义由提供方定义。

## 现有接入点

| 入口 | 实际职责 |
|---|---|
| `collector/adapters/<platform>.py: refresh(source)` | 刷新经校验的明文快照，失败保留旧快照 |
| `scan(source,start,end,...,context=None,conversations=None)` | 输出标准消息，支持时间窗、会话范围和有限邻居读取 |
| `collector/filters.py: scan_filtered` | 建公共消息表、运行注册 SQL、合并命中、维护连续性 |
| `collector/identities.py: IdentityContext` | 只给最终选中消息补身份资料和显示名 |
| `providers/platforms/assets.py: AttachmentArchive` | 兼容层的归档、定位及媒体解码；`collector/assets.py` 仅兼容导入 |
| `collector/store.py: Store` | 稳定消息身份、修订、事务、outbox 与原子批次发布 |
| `contracts.py` | 公共 `validate_event`、`ingest_batch`，校验与原子发布 |
| `providers/platforms/attachments.py` | 内置账号校验、实时附件定位和归档/解码；`collector/attachment_provider.py` 仅兼容导入 |
| `providers/registry.py` | 根据本地配置加载附件提供方，消息不能选择执行代码 |
| `providers/attachments.py` | 共用文件读取、页/块证据与覆盖状态 |
| `processor/core.py` | 导入标准 JSONL；不是平台解析入口 |

内置采集命令和调试台模块卡片仍只管理 `wechat/qq`。外部收集器使用 `contracts.ingest_batch` 发布标准事件，附件通过 `attachments.providers` 注册 `prepare_attachment`，无需修改处理核心；完整步骤见 [提供方接入](provider-api.md)。外部提供方自行管理采集日程和断点，接入不自动增加调试台采集卡片。

独立收集器也可以直接发布符合 v1 的不可变 JSONL。按需读取时，优先使用已有有效归档；没有归档则调用注册的提供方。只输出附件名字、未注册提供方且没有归档时，会明确返回不可用。

## 开发顺序

1. 用合成数据库固定平台字段映射，标清稳定 ID、时间单位、发送者和会话的区别。未知字段保留在 `extensions.<platform>`，不要推断为名字。
2. 实现只读捕获和校验。QQ 复用官方 SQLCipher，微信复用已提取的现有解密代码；不要另写密码引擎。
3. 实现标准消息扫描。窗口为 `[start,end)`；同一时间的消息有稳定排序。只读原始库；索引只能建在自有明文快照。
4. 接入公共 SQL 规则表。每条 SQL 只返回 `identity`，参数绑定，不允许写库、ATTACH 或加载扩展。各规则都查询完整输入，随后取并集去重。
5. 在筛选后补身份资料。只查选中发送者、明确 @/引用关联人和私聊对端；当前账号单列。生成 `identity` 与 `sender`，不输出全量联系人。
6. 接入附件定位和归档，验证消息归属、白名单、hash 与格式。低强度采集可仅留索引；显式工具读取必须尝试准备完整可用内容。
7. 使用既有 outbox 发布，验证重放、崩溃恢复、失败不推进断点，再接可选调试台。

## SQL 与历史触发点

公共表 `messages` 包含本轮及有限历史，`candidates` 只包含本轮时间窗。字段和规则注册细节以 [FILTERS.md](../FILTERS.md) 为准；不要另建一套 JSON 条件语言。

文件、全员提及、关键词各自独立注册。关键词只检查当条正文；上下文以文件或全员提及为中心取同会话前后 N 条。多个规则命中同一条消息时仅输出一份消息，保留全部实际规则来源。

旧文件在窗口外、其邻居在窗口内时，整段可能只标记本轮 `context.sql`。这不代表文件丢失或规则合并覆盖；先核对文件是否存在于 messages、`kind`、时间窗和原始规则结果，不要为“补标签”扩大文件规则的时间范围。

## 人物资料实现与限制

微信刷新包含 `contact/contact.db`。名称查 `contact`/`stranger`；群昵称复用上游 `chat_helpers.py` 的既有解析函数，来源见 [上游说明](../collector/vendor/wechat/UPSTREAM.md)。联系人缺失时返回明确缺失，不从聊天正文猜测。

QQ 群消息 `40090` 为群名片、`40093` 为昵称回退；近期会话表 `40094` 提供会话名称。私聊 `40093` 可能是昵称或备注，只记录为显示名。当前不承诺完整好友备注库或历史更名记录。自身昵称使用有界消息快照回退，找不到则标缺失；不能为补名字扫描并输出整个群。

单条消息的身份快照只含相关人，处理层在组装会话时再合并，且二次检查人物范围。`@所有人` 不展开成员。群昵称按会话保存；昵称、备注、名片分别保留，不用一个字段相互覆盖。

## 配置与调试

每个账号一份配置，使用独立 `storage.state_file`，可共享 `storage.output_dir`。相对路径相对配置文件解析；`source.account_label` 可填写“工作微信”等本地标签。扫描间隔、刷新间隔及解码强度的含义见 [项目 README](../README.md)。

```powershell
.\run.cmd once --config config/qq.toml --start 1794489581 --end 1794490260
```

这会真实发布该时间段的新增修订，但不推进正常扫描检查点。仅核对结构时先用调试台预览；执行处理会调用所配置模型。旧 JSONL 不可直接补写昵称，需重新扫描并生成新修订。

## 验收标准

| 场景 | 必须成立 |
|---|---|
| 窗口边界、同秒消息、跨分片 | 不漏边界，不混群，身份可复现 |
| 多规则命中、重复投递、重放 | 一条消息保留全部命中规则，重试不重复发事件 |
| 旧触发点＋新邻居 | 触发消息和上下文一起交付，AI 保留类型和附件索引 |
| 身份缺失、同名、多个群昵称 | 不猜测、不错误合并，只有上下文涉及的人进入 people |
| 原始库/WAL 变化、解密失败 | 源库不写入，保留旧快照，不推进成功检查点 |
| 归档已存在、低强度仅索引 | 优先复用；显式工具调用能够触发准备与解码 |
| 文件缺失、路径越界、格式不支持 | 明确返回不可用，不造正文，不使其他会话结果丢失 |
| 解析异常和后台运行 | 不泄露密钥，不执行文档指令，不弹终端抢焦点 |

```powershell
$env:PYTHONPATH = "$PWD/runtime-deps"
& C:/veighna_studio/python.exe -m pytest tests/test_filters.py tests/test_identities.py tests/test_pipeline.py tests/test_attachment_resolver.py -q
```

平台数据库变更另跑对应 `test_qq*` / `test_wechat*`；处理投影变更跑 `test_processor_context.py` 与 Pi 原生测试。真实样本只做本地核对，报告写数量和状态，不提交私人聊天、密钥或数据库。模拟模型通过不代表自然语言理解质量已验收。
