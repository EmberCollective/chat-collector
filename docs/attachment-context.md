# 附件作为处理层上下文

附件定位、解码和文件解析由提供方及共享 `providers/` 读取器完成，不调用 AI。处理层将索引和按需返回的证据交给 Pi。

## 本地读取与模型工具的边界

处理层为当前输入消息中每个附件分配 `f1`、`f2` 等短引用。已归档且 SHA-256 相同的内容只注册一次，同时保留所有消息来源与附件下标。模型只能调用：

```text
read_attachment(ref="f1", pages="1-3,5", query="截止")
```

`ref` 必须来自当前注册表；工具不接收本地路径、URL、命令或任意 SQL。优先校验并复用收集事件或本地解析记录中的归档。若事件只有引用，工具通过 `attachments.providers` 注册表，或兼容的内置 `attachments.collector_configs` 找到对应平台和账号，按消息 ID 与时间查询本地数据库更新附件定位，再复用收集器的定位、归档与解码代码。只有显式调用工具时才执行这一步；定时采集的 metadata 强度不限制本次按需解码。采集正在占用数据库时最多等待 45 秒，避免与数据库刷新并发读写。配置中禁止归档的账号仍返回明确错误。解析后的真实路径必须在配置的 `archive_roots` 内；若归档带 SHA-256，读取时再次比对。只有引用且已配置收集器的附件显示为“调用工具时自动准备”；调用后仍然缺失、解码失败或越界的附件才记录读取失败，不删除原消息。

工具返回不含原始路径。页/块证据如 `f1:p4`、`f2:b3` 仅在确实提供了文本或视觉图块后登记；没有视觉能力时的图片不构成可读证据。最终来源、收集模块、消息 ID、附件下标和归档信息应由主处理程序从本地映射补齐，不能采信模型自行编写的来源字段。

## 格式与成熟依赖

| 格式 | 读取方式 | 证据单位 |
|---|---|---|
| PDF | PyMuPDF 逐页提取文本；扫描页和含图片页可按需渲染 | `p1`、`p2` 等原页号 |
| DOCX | python-docx 按正文段落、表格的实际顺序读取；内嵌图片独立成块，追加页眉页脚 | `b1`、`b2` 等顺序块号，不冒充版面页码 |
| DOC / PPT(X) / XLS(X) | 已安装 LibreOffice，在独立用户配置下隐藏转换为 PDF，再由 PyMuPDF 读取 | 转换后 PDF 页号 |
| TXT / Markdown / CSV / JSON / XML / HTML | 标准文本解码后分块，HTML 作为文本，不执行脚本 | `b1` 等块号 |
| 图片 | Pillow 校验尺寸、处理方向、缩小并输出 PNG；多帧图片逐帧列出 | `p1` 等帧号 |

本机检查结果：PyMuPDF 2.0、python-docx 1.2、Pillow 13、LibreOffice 26.2 已安装。未发现 Tesseract、RapidOCR、EasyOCR 或 PaddleOCR；没有安装另一套 OCR，也没有手写解析器或解密算法。依赖清单位于 `processor/requirements.txt`，不加入收集层的必需依赖。

扫描 PDF 和图片在 `model.vision=true` 时作为视觉图块提供。模型不支持视觉且无本地 OCR 时返回 `visual_content_requires_vision_model_or_local_ocr`，必须待复核，不能把它解释成“附件没有任务”。DOCX 的脚注、尾注或不可解码内嵌图片也明确报告限制；不执行宏、不跟随外部图片链接，不提取内嵌可执行对象。

## 长文档与覆盖范围

没有 `pages` 和 `query` 时，一次只返回前三个单位；返回值同时列出总页/块数、本次选择范围、累计完整读取范围和剩余范围。模型根据待办判断需要继续读取哪些内容；未读范围由本地审阅记录保留。不能把第一页或前三页当作全文。

`pages="1-3,5"` 指定 PDF 页号、图片帧号，或 DOCX 块号。一次最多 10 个单位、3 张视觉图片、20,000 字文本。`query` 是不执行正则表达式的字面搜索，命中后返回附近片段；片段不算整块已读。文本搜索未命中也不代表扫描图片中不存在该内容。

返回 `details` 包含：

```json
{
  "status": "ok",
  "attachment_ref": "f1",
  "unit": "page",
  "total_units": 8,
  "selected_units": [1, 2, 3],
  "read_units": [1, 2, 3],
  "remaining_units": [4, 5, 6, 7, 8],
  "complete_document": false,
  "warnings": [],
  "evidence": [{"ref": "f1:p1", "coverage": "text"}]
}
```

`status=ok` 只说明本次工具调用成功；是否检查全文必须看 `complete_document`。有图片但没有向视觉模型提供图片的页面不会进入累计完整读取范围。不要求模型完整读完所有附件。未选择读取、主动跳过、部分读取、读取完成和读取失败分别记录；允许输出空待办，其含义仅为已检查内容未发现待办。失败附件的相关任务标记复核，不影响其他会话保存。无关且未请求的附件不会给全部待办附加复核标记。

## 配置与缓存

在处理层配置中使用独立分区，路径相对处理层配置文件解析：

```toml
[attachments]
enabled = true
archive_roots = ["../data/assets"]
cache_dir = "../data/processor/attachment-cache"
max_file_bytes = 33554432
max_pages = 200
max_pixels = 20000000
max_text_chars = 1000000
max_blocks = 2000
timeout_seconds = 45
max_memory_mb = 1024

[attachments.collector_configs]
qq = "qq.toml"
wechat = "wechat.toml"

[model]
vision = false
```

缓存按内容 SHA-256、解析格式版本和限制配置隔离，包含本地输入副本、提取文本与渲染图片。重复内容复用缓存。不同限制配置重新解析，避免用旧结果绕过新限制。缓存含私人正文，应留在本机、排除版本控制；删除缓存只导致下次重建，不应删除收集层原归档。

解析和转换运行在隐藏的独立进程中，监测进程树内存和时间上限；图片尺寸、PDF 页数、DOCX 展开体积、文本总量也有限制。LibreOffice 使用临时独立配置，采用最高宏安全等级、禁止自动更新文档链接，不复用用户正在编辑文档的 Office 进程。进程隔离和内存监测不是完整的操作系统安全隔离；仍应及时更新本地解析库和转换器。

## 开发接口与验证

```python
from providers.attachments import AttachmentContext

context = AttachmentContext(group["messages"], config)
catalog = context.catalog()
tool_result = context.read_attachment("f1", pages="2-4")
verified_evidence = context.evidence  # 仅实际读取过的短证据引用
```

工具输出采用 Pi 原生 `content`/`details` 结构，图块为 `type=image`、`mimeType=image/png`、Base64 `data`。本地 `catalog.origins` 与 `evidence[ref].origins` 包含 `message_key` 和 `attachment_index`，用于最终来源回填；发送给模型的目录应移除内部映射等不必要信息。

合成测试覆盖多页 PDF、末页关键词、DOCX 段落/表格顺序、内嵌图片、图片/扫描页视觉降级、内容缓存、越界路径和哈希不符、体积/页数/像素限制、重复附件来源，以及本机真实 LibreOffice DOC 转换。测试不向外部模型发送真实内容：

```powershell
python -m pytest tests/test_processor_attachments.py -q
```

## 本地审计与恢复

原始模型提交在来源校验前保存到处理状态库同级的 `submissions/`，包括短引用映射和附件覆盖信息，不包含 API 凭据。各会话的任务及附件审阅状态独立写入 `group_results`；即使任务为空也保留记录。自动准备的附件解析映射保存在 `attachment-cache/resolved/`，后续运行可直接校验并复用归档，避免重复解码。上述审计、草稿和来源映射均不注入 Pi 的上下文；会话和压缩继续完全由 Pi 原生 SDK 管理。

本机未下载的附件不会凭数据库元数据生成正文；越界路径、缺失源文件、格式不支持和解码失败会通过工具返回具体状态。不会执行 EXE/JAR 或解包任意压缩包。

自定义附件提供方的配置及函数签名见 [提供方接入](provider-api.md)。旧 `processor.attachments.AttachmentContext` 导入仍保留兼容；解析进程现位于 `providers/attachment_worker.py`，资源限制和隐藏启动方式不变。
