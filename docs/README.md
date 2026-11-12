# 项目文档

| 目标 | 文档 |
|---|---|
| 理解模块与运行入口 | [开发指南](development.md) |
| 接入新的收集模块 | [数据契约](data-contract.md)、[收集层开发指南](collector-development.md) |
| 检查模型实际收到什么 | [AI 输入结构](ai-context.md)、[合成输入](examples/ai-context-v1.json) |
| 编写 SQL 筛选 | [规则集](../FILTERS.md) |
| 调整处理与待办规则 | [处理层](processing.md)、[待办查询](task-search.md) |
| 理解附件工具 | [附件上下文](attachment-context.md) |
| 调试 Pi 与本地面板 | [Pi 运行时](pi-runtime.md)、[处理调试台](console-processing.md) |
| 排查采集性能 | [优化方法](optimization.md)、[微信性能](wechat-performance.md) |

合成示例不含真实聊天、凭据或本机绝对路径。接入契约与实际 AI 投影分别维护；尚未实现的通用提供方注册接口已在契约中明确标注。
