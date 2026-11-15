# 处理层

处理层消费标准消息事件，组装 AI 上下文，通过 Pi 生成有证据的待办，并管理任务状态。

| 分类 | 文档 |
| --- | --- |
| 采集层要求契约 | [标准事件输入](input-contract.md) |
| Pi 组件 | [运行时与工具](pi/README.md) · [调试与运行记录](pi/console.md) |
| 处理层输出契约 | [行动、证据与材料](output-contract.md) |
| 配置与输入检查 | [处理配置](processing.md) · [AI 实际输入](ai-context.md) |
| 任务接口 | [待办查询](task-search.md) · [状态变更](task-status-api.md) |

平台专有读取在 [兼容层](../collector/compatibility/README.md) 完成。任务变更交给 [信息调度层](../information/dispatch/README.md) 消费，不让渠道直接改正式任务表。
