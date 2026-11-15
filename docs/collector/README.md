# 收集层

收集层维护经过校验的来源快照；兼容层将来源转换为标准消息，供筛选与处理使用。

| 分类 | 内容 |
| --- | --- |
| [采集](acquisition/README.md) | 刷新、增量捕获、快速查询与性能 |
| [兼容层](compatibility/README.md) | 平台字段、只读规则查询、身份、附件与新来源 |

跨层交付遵循 [处理层输入契约](../processor/input-contract.md)。各包的职责以 [架构边界](../architecture/README.md) 为准。
