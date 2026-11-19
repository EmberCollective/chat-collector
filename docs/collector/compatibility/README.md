# 兼容层

兼容层由 `providers` 实现：平台数据库映射成标准消息，用户 SQL 在统一只读环境执行，附件按需定位和解析。采集器无需执行 SQL 规则，处理层无需理解 QQ、微信的表结构。

| 目标 | 文档 |
| --- | --- |
| 了解快照、扫描、SQL 与缓存的实现 | [查询实现方法](query-implementation.md) |
| 接入新的消息或附件提供方 | [提供方接入](provider-api.md) |
| 配置学习通只读采集与 SQL 筛选 | [学习通来源](chaoxing.md) |
| 理解附件工具与证据范围 | [附件上下文](attachment-context.md) |
| 编写跨来源 SQL | [规则契约](../../../FILTERS.md) |

只读取现有消息的操作步骤见 [快速查询](../acquisition/fast-query.md)。原 `collector.filters/store/identities` 等兼容导入继续保留，新代码使用 `providers`、`selection` 的实际入口。
