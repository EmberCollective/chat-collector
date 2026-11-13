# 接入一个新的消息提供方

提供方交付标准事件，负责原始数据库、身份资料和附件定位/解码；处理层不读取新平台的原始字段。现有 QQ、微信配置无需改动。新增收集器不必注册新的 Pi 工具，也不需要另写上下文循环。

## 发布消息

在项目 Python 环境中调用：

```python
from contracts import ingest_batch, validate_event

validate_event(event)  # event 结构见数据契约及 docs/examples/provider-event-v1.json
published = ingest_batch([event], "D:/opt/chat-collector/data/batches")
```

`ingest_batch(events,inbox)` 接收迭代器，返回已发布 `Path`，空输入返回 `None`。先校验整批，再写入同目录临时文件、同步并原子发布；处理中不会看到半个 JSONL。使用支持硬链接的本地文件系统（本机 NTFS）；不支持时明确失败，不降级为可能覆盖旧批次的写法。

同批重复事件只保留一份，相同批次重试复用同一文件。不同批次重复事件由处理层 `event_id` 去重；同消息新版本必须提高 `revision`。同批冲突立即拒绝，跨批冲突在处理层导入时拒绝。提供方负责稳定身份和修订；接口不自动改名、不生成修订、不调用 AI，也不推进收集检查点。每批建议不超过 1000 条，过大则由提供方分批。

内置采集器仍使用 `Store` 的事务与 outbox。没有必要先写 outbox 再调用这个入口；它用于外部收集器的完整事件发布。

## 注册附件准备函数

已有有效归档可直接按标准附件字段提供，无需插件。只有元数据时，在处理配置加入：

```toml
[attachments.providers.demo]
factory = "examples.local_file_provider:LocalFileProvider"
config = "../examples/provider.toml"
```

平台注册键要与 `message.platform` 相同。路径相对处理配置；工厂模块须可由本地 Python 导入。工厂来自用户维护的配置，不从消息、附件或模型参数解析模块名。同平台不能同时出现在 `providers` 与旧 `collector_configs` 中。

工厂接口是普通 Python：

```python
class MyProvider:
    def __init__(self, config_path: str, cache_dir: str):
        ...

    def prepare_attachment(self, event: dict, index: int) -> dict:
        # 核对平台、账号、消息与附件，复用归档，否则定位/解码并归档。
        # 返回原附件字段加 archive_path、sha256、status；失败可抛固定错误码。
        ...
```

只在 AI 首次读取未归档附件时创建实例并调用。`index` 是该消息 `attachments` 的原始下标；模型只看到 `f1`，不传路径或下标。提供方返回的文件仍必须通过全局归档目录、大小、内容摘要和格式检查。模型无权通过插件读取任意路径。未知插件、账号错误、文件缺失等以工具失败返回，不变成空正文。

保留原附件的 `name/md5/size_bytes`，不要把其他文件冒充请求附件；插件结果中的已知身份字段会再次核对。文件内容与消息的语义归属最终仍需提供方保证，文件摘要不能证明两个业务对象相同。

微信图片的资源定位摘要可能与消息中声明的 MD5 不同。内置提供方将定位值另存为本地 `resource_md5`，保留原 `md5`，解码后仍用 `sha256` 验证归档内容。缓存复用也核对原附件身份，旧错误缓存会重新解析，不能跳过身份校验。

`local_source_not_found` 表示配置允许的本地位置没有对应文件；工具不会凭消息中的文件名生成正文，也不会自动从 QQ/微信云端下载。先在客户端下载需要的原附件，再重新运行调试。语音和视频目前没有转写/理解能力，会明确返回 `audio_video_understanding_unavailable`，不再误报为普通文件找不到。

内置实现是 `collector.attachment_provider.CollectorAttachmentProvider`，保留账号校验、锁等待、已有归档复用和完整强度按需解码。公共 PDF、Office、图片读取在 `providers/` 中复用成熟依赖。新增平台通常只实现准备函数，文件解析、证据页码和 Pi 响应格式继续共用。

## 可运行的本地文件示例

[local_file_provider.py](../examples/local_file_provider.py) 只从配置中查找附件编号，适合验证外部平台接入。创建自己的 `examples/provider.toml`：

```toml
platform = "demo"
account_id = "demo-self"

[files]
notice = "../data/assets/demo-notice.pdf"
```

消息的 `platform/account_id` 对应配置，附件可为：

```json
{"type":"file","name":"demo-notice.pdf","provider_ref":"notice"}
```

将测试文件放到配置的归档目录，再发布符合契约的事件。预览只能看到附件索引，首次 `read_attachment("f1")` 才加载提供方并解析。`provider_ref` 留在本地，不发送给 AI。示例直接引用已归档文件；真实平台在同一函数中接入自己的定位和解码即可。

插件是本地受信任代码，具备当前 Python 进程权限；注册表并不是代码隔离环境。只安装自己审阅的提供方。解析进程有时间/内存限制并隐藏启动，但这些限制不等于完整操作系统隔离。

## 平台事实与调度边界

`message.extensions.<platform>` 保存私有字段，默认不进入 AI；`message.context_notes` 只放契约允许的简短事实，类型和长度见 [数据契约](data-contract.md)。人物仍由提供方查询，且只包含选中消息涉及的人。处理层只分配短引用，不补查新平台联系人。

外部提供方自行运行采集、筛选和日程；面板采集卡片只管理内置 QQ、微信，`input.refresh_collectors` 已停用，处理不隐式启动采集。附件注册不会执行定时扫描。外部消息可在通用处理预览中查看，任务来源仍保留平台、账号、会话和原消息证据。

验证：`tests/test_contracts.py` 覆盖发布、并发重试和处理去重；`tests/test_providers.py` 使用第三平台示例验证按需读取、账号隔离及目录约束。没有模拟新平台的真实密码算法。
