<p align="center">
  <img src="assets/readme/logo.svg" width="88" height="88" alt="拾序 · Loose Ends Logo：聊天气泡中的完成勾">
</p>

<h1 align="center">拾序 · Loose Ends</h1>

<p align="center"><strong>把零散信息，理成下一步。</strong></p>
<p align="center">本地采集 · SQL 筛选 · 自定义规则 · 多渠道提醒</p>

<p align="center">
  <a href="https://github.com/EmberCollective/loose-ends/actions/workflows/ci.yml"><img src="https://github.com/EmberCollective/loose-ends/actions/workflows/ci.yml/badge.svg" alt="CI 状态"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="FILTERS.md">SQL 筛选</a> ·
  <a href="processing-rules/default.md">待办规则</a> ·
  <a href="docs/README.md">完整文档</a>
</p>

从不同来源的信息中整理个人待办，保留材料与来源。内置微信、QQ 本地采集，支持接入其他消息来源，并将结果交给飞书、Notion 或自己的应用与通知平台。

> **⚠️ 实验性项目。** 本项目仅在有限的 Windows x64 环境验证，不保证在任意机型、系统版本或微信/QQ 客户端版本上可用，也不保证取钥、解密、增量刷新、采集和附件读取始终正常运行。客户端或系统更新可能使相关功能随时失效；请先备份数据，并在非关键环境中验证。

## 看看效果

**飞书卡片与附件（合成测试）**：展示行动说明、随附材料，以及“完成待办 / 稍后提醒”入口。

![飞书合成测试卡片：行动说明、完成待办和稍后提醒按钮，下方附带 connection-test.txt 文件](assets/readme/feishu-card.png)

<details>
<summary>查看本地调试台：运行概览、自动化与 Pi 工作流</summary>

以下为已实现页面使用合成展示数据的截图，不代表本机真实运行情况。

![拾序运行概览：当前故障、后台状态、入库变更、采集成功率与趋势](assets/readme/console.png)

![拾序自动化：分类配置投递、提醒、监听与目标通道](assets/readme/console-automation.png)

概览先显示故障与结果未知，再核对服务、积压、采集条数和耗时；来源占比可切换为文件逻辑大小占比。自动化统一配置采集与处理、投递与提醒、后台服务；SQL 规则按目录发现并勾选。运行记录可按操作、模块、触发方式和耗时筛选，Pi 工作流可追查上下文、模型可见输出与工具调用。

</details>

## 为什么用它

- **本地解密，减少账号交互**：读取本机微信、QQ 数据库，不通过聊天账号模拟收发消息。采集与筛选不使用 AI；账号风险边界见下文。
- **各层解耦，方便接入**：采集、处理、消息投递可独立运行；通过统一契约接入新的消息平台、自定义收集器和附件提供方，也可通过 Webhook 或 Python 适配器将待办接入自己的应用、工作流或通知平台。
- **增量处理，节省解密与 Token 开销**：首次完整解密，后续复用快照、刷新变化页；只为选中消息处理附件，模型按需读取内容，未变化的分组不重复调用模型。
- **直接写 SQL 筛选**：用 SQLite 原生查询表达群聊、关键词、文件和上下文条件，多条规则合并去重；支持预览与重放，无需学习额外的筛选语法。详见 [SQL 规则](FILTERS.md)。
- **消息与待办都有来源**：保留原消息身份、会话、时间和命中规则；待办关联支撑它的消息与附件证据，方便回查依据。
- **按你的习惯安排待办**：直接编辑 [Markdown 规则](processing-rules/default.md)，自定义什么算待办、P0–P4 优先级、时间与材料提取；提醒频率也可单独调整。

流程：**本地聊天库 → SQL 筛选 → 自定义规则 + 模型 → 带来源的待办与材料 → 通知平台**。

## 快速开始

当前提供 **Windows x64 本地使用预览包**，安装需要联网。

1. 按 [安装指南](docs/guides/installation.md) 准备预览包，解压后双击 `Install.cmd`，选择空目录安装。无需预装 Python、Node 或编译器。
2. 按引导选择微信 / QQ，完成首次取钥，填写模型配置；飞书可选，稍后也能接入其他渠道。
3. 引导首次处理最近 7 日符合 SQL 规则的消息；日常双击 `Start.cmd`，在本地调试台查看状态、预览筛选和调整规则。

源码安装与分层运行见 [开发指南](docs/guides/development.md)。源码目录的 `Install.cmd` 使用已有发布 ZIP，缺包时需先按安装指南构建。项目版本统一由 [version.json](version.json) 管理，发布清单和本地调试台读取同一版本号。

## 按需调整与扩展

| 想做什么 | 从这里开始 |
|---|---|
| 只收集指定群、关键词、文件或上下文 | [SQL 规则](FILTERS.md) |
| 自定义待办判断、优先级与模型 | [默认规则](processing-rules/default.md) · [处理配置](docs/processor/processing.md) |
| 配置飞书、Notion、提醒与操作反馈 | [渠道配置](docs/information/channels/configuration.md) · [飞书设置](docs/information/channels/feishu-setup.md) |
| 接入其他消息来源或通知平台 | [收集层开发](docs/collector/acquisition/development.md) · [数据契约](docs/processor/input-contract.md) · [渠道开发](docs/information/channels/development.md) |
| 设置自动运行、失败重试与恢复 | [自动化](docs/information/dispatch/automation.md) |
| 让 AI 客户端通过本地 MCP 搜索、修改待办并查看提醒 | [MCP 服务](mcp_service/README.md) |
| 快速查询消息、分析索引与采集性能 | [查询方法](docs/collector/acquisition/fast-query.md) · [查询实现](docs/collector/compatibility/query-implementation.md) |

## 使用边界

- **数据授权**：仅处理你本人或已获明确授权的本地聊天与附件；保护其他参与者的隐私，并遵守微信、QQ 及所用服务的平台协议与适用法律。本项目不代表腾讯或相关上游项目，引用其名称不表示官方授权或背书。
- **账号与兼容性**：本地解密减少了与平台服务的交互，但没有封号率实测，不能保证零风险。首次取钥涉及微信 Hook 或 QQ 进程内存读取；客户端升级可能影响兼容性。
- **隐私与费用**：配置远程模型后，选中消息及实际读取的附件会发到该服务；启用通知渠道后，相应待办和材料会离开本机。修改待办规则可能重新处理历史分组并产生费用。
- **结果与数据**：模型可能误判，需结合来源核对。聊天、附件和凭据均属敏感数据，请保管好 `.env`、`secrets/` 与 `data/`；已有部署请勿用新包直接覆盖。
- **项目许可与分发**：当前仓库是实验性源码预览，尚无覆盖整个项目的许可证，不应视为 MIT 或已获自由再分发许可。微信提取与取钥组件仍有授权缺口，QQ 解析器为 GPLv3，PyMuPDF 为 AGPLv3／商业双许可；MIT 目标的清理路径见 [许可与再分发](docs/guides/licensing.md)，来源与原文见 [第三方说明](THIRD_PARTY_NOTICES.md)。以上使用边界不能替代第三方许可或平台授权。

更多说明见 [文档索引](docs/README.md)，增量机制与实测见 [增量刷新](docs/collector/acquisition/incremental-refresh.md)。
