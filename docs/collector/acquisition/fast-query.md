# 快速数据库查询

先缩小读取范围，再看查询计划。日常程序优先使用兼容层的标准消息接口；只有排查字段、索引和原始映射时，才直接读取平台明文库。

以下 Python 示例从仓库根目录运行，使用已安装项目依赖的 Python 环境。它们不刷新来源、不推进筛选断点、不调用模型，也不发送通知。

## 按目标选择入口

| 想知道什么 | 读取位置 | 成本与边界 |
| --- | --- | --- |
| 上次采集何时完成、哪份快照 | `collector.service.publication_path(config)` | 读取小型 `.collection.json`，适合状态展示；不代表筛选入库条数 |
| 某时间段有哪些标准消息 | `providers.query_registry.query_snapshot(scope)` | 使用已有快照，窗口与会话有界；不执行用户规则 |
| 哪些消息命中多条 SQL | `providers.query.query_rules(scope, window, rules)` | 建本轮临时统一表、只读执行规则，合并全部命中 |
| 平台查询有没有用索引 | 已发布明文库上的 `EXPLAIN QUERY PLAN` | 诊断用途，绑定参数、只读打开，避免正文和附件大字段 |
| 面板成功率、运行耗时 | 自动化运行库与控制台任务记录的摘要接口 | 不是全量聊天库计数；口径见 [运行概览](../../design/runtime-monitoring/README.md) |

## 只查采集进度

```python
import json
from collector.config import load_config
from collector.service import publication_path

config = load_config('config/qq.toml')
path = publication_path(config)
if path.is_file():
    saved = json.loads(path.read_text(encoding='utf-8'))
    print({key: saved.get(key) for key in
           ('snapshot_id', 'capture_started_at', 'collected_at')})
else:
    print('尚无采集发布记录')
```

该文件是状态摘要。要保证读取期间快照未变化，使用下一节的快照上下文；不要直接读摘要后就假定数据长期不变。没有快照时，先按正常采集流程完成采集，查询函数不会隐式刷新。

## 读取最近五分钟的标准消息

```python
from contextlib import closing
from itertools import islice
import time
from collector.config import load_config
from providers.query_registry import query_snapshot

config = load_config('config/qq.toml')  # 微信使用自己的配置文件
scope = dict(provider_id=config.get('provider_id', config['platform']),
             platform=config['platform'],
             account_id=config['source']['account_id'], config=config)
end = int(time.time())
with query_snapshot(scope) as snapshot:
    with closing(snapshot.scan(end - 300, end)) as rows:
        sample = list(islice(rows, 20))
    print(snapshot.snapshot_id, len(sample))
    for message in sample:
        print(message['sent_at'], message['message_id'], message['kind'])
```

窗口为 Unix 秒的 `[start,end)`。这是从已有快照读取的最多 20 条样本，不能当作窗口总数；适配器可能需要排序或解析更多行，`islice` 不保证底层仅读 20 行。更小的时间窗，以及 `scan(..., conversations={('group', '会话ID')})`，能进一步限定来源。提前停止迭代时关闭生成器，再退出快照锁；不要在锁内等待模型或网络。

新来源通过注册快照提供方复用同一入口，见 [查询实现方法](../compatibility/query-implementation.md)。不要在业务层增加 QQ／微信分支。

## 检查 QQ 明文库的索引计划

只读取配置中的公开 `nt_msg.db`。`.incremental/qq-native.db` 是采集器私有页面镜像，大库使用 QQ 特有布局，不能用普通 `sqlite3` 打开、修复或加索引。微信对齐镜像也不能随意加索引。

```python
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from collector.config import load_config
from collector.service import source_snapshot

config = load_config('config/qq.toml')
path = (Path(config['source']['decrypted_dir']) / 'nt_msg.db').resolve()
sql = '''SELECT rowid, "40050" AS sent_at
         FROM group_msg_table
         WHERE "40050">=? AND "40050"<?
         ORDER BY "40050",rowid LIMIT ?'''
end = int(time.time())
params = (end - 300, end, 50)
with source_snapshot(config):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        plan = db.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
        rows = db.execute(sql, params).fetchall()
        print(plan, len(rows))
```

群消息为 `group_msg_table`，私聊为 `c2c_msg_table`，时间列 `40050` 为 Unix 秒。正常计划可见 `_collector_time_group_msg_table` 的时间范围 `SEARCH` 或覆盖索引；`SCAN` 可能代表全表扫描，`USE TEMP B-TREE` 代表额外排序。以本机实际计划为准，不能只看索引名字就判断快慢。动态表名不能作为 SQL 值绑定，须从固定允许集合选择；条件值始终用参数。

连续翻页使用上一页最后的 `(sent_at,rowid)` 作为游标，在同一个快照上下文内执行：

```sql
SELECT rowid, "40050" AS sent_at
FROM group_msg_table
WHERE "40050">=:start AND "40050"<:end
  AND ("40050",rowid)>(:last_time,:last_rowid)
ORDER BY "40050",rowid
LIMIT :page_size;
```

首屏使用前一个无游标的查询。后续页绑定上一屏的时间和行号，避免同秒消息漏读；不要用越来越大的 `OFFSET`。跨请求分页需另存并核对 `snapshot_id`，快照变化后重新查询。

## 索引与缓存怎么选

| 情况 | 推荐做法 |
| --- | --- |
| 首页只展示计数与运行状态 | 复用后端摘要和有界缓存，避免每次加载全量消息 |
| 反复读同一来源、同一时间窗 | 缓存键包含来源、账号、`snapshot_id`、窗口和查询参数；快照改变即失效 |
| 条件是 `datetime(时间列)`、`CAST(时间列)` | 尽量将边界转换为原始数值，再用时间列范围比较，保留索引可用性 |
| 仅需判断是否存在 | `SELECT 1 ... LIMIT 1`，避免全表 `COUNT(*)` |
| 只查看时间与 ID | 只选所需列，避免 `SELECT *` 与附件 BLOB |
| 已有主键／时间索引命中 | 先测读取、排序、解码与校验耗时，不重复建索引 |
| 独立自有查询库缺少高频索引 | 根据实际 `WHERE`、排序和查询计划建索引，计入写入与空间成本 |
| 微信对齐镜像或 QQ 私有增量镜像 | 保留源页面结构，不添加普通业务索引 |

现有标准 SQL 临时库按一轮创建，不是持久查询缓存。SQL 的 `LIMIT` 只限制规则命中数，不能减少此前的来源扫描和临时表构建。面板摘要缓存实现见 [加载优化](../../design/runtime-monitoring/IMPLEMENTATION.md)，QQ 已验证的索引与读取瓶颈见 [刷新实测](qq-refresh-research.md)。

## 快慢如何验证

对同一快照、同一参数分别记录扫描、解码、规则执行和总耗时；区分首次读取与系统热缓存。先看计划，再缩小窗口或投影，最后考虑索引。不要把无变化轮次和有变化轮次直接比较为倍数提升。

合成库可以验证窗口边界、同秒游标、只读限制和计划是否使用索引；真实机械盘吞吐、源库并发变化和扩容校验仍需实测。诊断不应自动执行 `VACUUM`、全库 `COUNT(*)`、`quick_check` 或刷新；完整性校验由采集发布流程负责。
