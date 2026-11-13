# 项目文档

| 目标 | 文档 |
|---|---|
| 理解模块与运行入口 | [开发指南](development.md) |
| 接入新的收集模块 | [数据契约](data-contract.md)、[收集层开发指南](collector-development.md)、[提供方接入](provider-api.md) |
| 检查模型实际收到什么 | [AI 输入结构](ai-context.md)、[合成输入](examples/ai-context-v1.json) |
| 编写 SQL 筛选 | [规则集](../FILTERS.md) |
| 调整处理与待办规则 | [处理层](processing.md)、[待办查询](task-search.md) |
| 整理用户怎么做与参与人员 | [行动输出](actionable-tasks.md) |
| 配置提醒、材料与成果 | [消息层契约](messaging.md)、[飞书 / Notion / Notion Calendar](messaging-channels.md) |
| 开发新的消息渠道与回调 | [渠道开发指南](channel-development.md) |
| 在面板管理通道、测试发送和回调 | [通道配置与合成测试](messaging-management.md) |
| 创建飞书应用、配置群和权限 | [飞书应用配置](feishu-setup.md) |
| 接入完成、恢复与取消操作 | [反馈队列与公开状态 API](messaging.md#操作反馈与状态回写) |
| 启动全链路自动化、处理失败 | [自动运行、重试与报警](automation.md) |
| 理解附件工具 | [附件上下文](attachment-context.md) |
| 调试 Pi 与本地面板 | [Pi 运行时](pi-runtime.md)、[处理调试台](console-processing.md) |
| 排查采集性能 | [优化方法](optimization.md)、[微信性能](wechat-performance.md) |

任务通过程序校验后直接入库；缺失信息用 `notes` 说明，不设人工复核流程。修订号保留在事件协议里用于防重复和冲突检测，业务界面不展示。

合成示例不含真实聊天、凭据或本机绝对路径。接入契约与实际 AI 投影分别维护；外部提供方通过公开发布接口和附件注册表接入。调试台是可选工具，自动运行由独立 `automation` 包负责；模型处理和外部投递的启用状态以本机配置为准，开发测试使用独立配置。
