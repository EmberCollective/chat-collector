# 项目文档

按收集、处理、信息交付三层查找文档。目录分类不改变代码所有权：SQL 查询复用 `providers`，筛选与发布归 `selection`，自动化跨层调度，渠道负责呈现和投递。

| 要完成的事 | 从这里开始 |
| --- | --- |
| 安装、启动、升级 | [安装指南](guides/installation.md) · [安装验收](guides/installation-acceptance.md) |
| 从源码运行、定位模块 | [开发指南](guides/development.md) · [架构边界](architecture/README.md) |
| 快速读取消息、判断是否需要索引 | [快速数据库查询](collector/acquisition/fast-query.md) |
| 编写筛选 SQL | [规则与参数](../FILTERS.md) |
| 配置模型、待办与提醒 | [处理配置](processor/processing.md) · [渠道配置](information/channels/configuration.md) · [自动运行](information/dispatch/automation.md) |
| 接入新来源或新渠道 | [提供方接入](collector/compatibility/provider-api.md) · [渠道开发](information/channels/development.md) |

## 收集层

- [采集](collector/acquisition/README.md)：快照刷新、快速查询、增量机制与性能排查。
- [兼容层](collector/compatibility/README.md)：平台字段适配、只读 SQL、身份资料、附件准备与新来源接入。

## 处理层

- [采集层输入契约](processor/input-contract.md)：标准事件、身份、时间、附件与幂等约束。
- [Pi 组件](processor/pi/README.md)：上下文、工具调用、压缩、重试与运行记录；[调试台](processor/pi/console.md)。
- [处理层输出契约](processor/output-contract.md)：行动、参与人员、证据与材料。
- [处理配置与任务接口](processor/README.md)：模型配置、AI 输入、待办查询与状态回写。

## 信息层

- [信息调度层](information/dispatch/README.md)：消费任务变更、通知、提醒、重试与自动运行。
- [信息调度层契约](information/dispatch/contract.md)：输入输出、投递包、材料、回执与反馈。
- [渠道层](information/channels/README.md)：飞书、Notion、日历配置、测试、回调与扩展。

## 架构、示例与设计记录

- [架构与数据所有权](architecture/README.md)、[批次契约](architecture/batch-contracts.md)。
- 架构实施记录：[实现与迁移](architecture/implementation.md)、[审查与验收](architecture/audit.md)、[任务分工](architecture/agent-tasks.md)。这些记录有各自基线，不能代替当前部署状态。
- 合成示例：[提供方事件](examples/provider-event-v1.json)、[AI 输入](examples/ai-context-v1.json)。
- 面板设计与验收：[整体重写](design/console-redesign/README.md)、[运行概览](design/overview-dashboard/README.md)、[负载与加载优化](design/runtime-monitoring/README.md)。

任务通过程序校验后直接入库；缺失信息记入 `notes`。模型与外部投递是否启用，以本机配置为准。使用边界与第三方授权见 [项目说明](../README.md) 和 [第三方说明](../THIRD_PARTY_NOTICES.md)。
