# 采集

采集负责稳定捕获、完整性校验、明文快照与采集进度。需要查询时先复用已发布快照；刷新是单独的写操作。

| 目标 | 文档 |
| --- | --- |
| 读状态、限定时间查消息、查看索引计划 | [快速数据库查询](fast-query.md) |
| 接入来源、核对字段与验收 | [收集层开发](development.md) |
| 理解页面增量、WAL 与失败恢复 | [增量刷新](incremental-refresh.md) |
| 找到性能瓶颈 | [优化方法](optimization.md) |
| 排查 QQ 快照刷新 | [QQ 查询计划与刷新实测](qq-refresh-research.md) |
| 排查微信读取 | [微信性能](wechat-performance.md) |

查询实现与扩展点见 [兼容层](../compatibility/README.md)，交付结构见 [输入契约](../../processor/input-contract.md)。
