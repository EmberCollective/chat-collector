# 筛选种类与上下文规则

普通条件和新规则共用 `collector/filters.py` 的类型注册器。CLI、正式采集、重放和控制台预览使用 `scan_filtered()`，不各自实现筛选。没有配置 `filters.rules` 时保持原有行为；只有点条件时直接查询源快照，只有涉及上下文时才构建临时 SQL 查询表。

## 使用上下文筛选

面板 → “筛选与重放” → “上下文筛选”：填写向前、向后的条数，选择关键词、文件或 @所有人，点击“加入扩展规则”，先预览。正式采集前到配置页保存。

示例：当前消息自身或前后各 2 条中，有消息包含“截止”，则选中当前消息。

```json
[
  {
    "type": "nearby",
    "before": 2,
    "after": 2,
    "condition": {"type": "keywords", "values": ["截止"]}
  }
]
```

这是“判断当前消息的邻近消息是否命中”的语义，直接命中的当前消息也保留。前后方向针对当前消息的邻居，不是针对命中点向外扩张的方向。`before=0, after=0` 只判断当前消息。先按消息时间，再按数字本地 ID、数据库名、消息 ID 稳定排序；同一会话的微信消息跨分库合并排序。QQ 群聊与私聊即使数字 ID 相同也不混合。

**上下 N 条计数包含不满足条件的消息。** 上方基础筛选限制输出的当前消息，不提前删除它的邻居。例如“当前消息为文件，前后两条中有截止关键词”可用基础 `files=true` 配合上述规则。若希望保留所有上下文消息，不要再用基础关键词把当前消息限制为必须包含该词。

`rules` 数组内各项为 AND；需要 OR 时使用 `any`。条件可嵌套；方向条数为 0–1000 的整数，树深最多 8 层、节点最多 128 个，所有上下文算子的累计向前/向后范围各最多 1000 条。超出或拼写错误会报错，不会默默忽略。

## 可用种类

| type | 参数 | 判断 |
|---|---|---|
| keywords | values: 字符串数组 | 文本包含任一关键词；沿用 SQLite lower/instr 语义 |
| kind | values: 字符串数组 | 消息类别属于列表，如 file、image、text |
| conversation | values: 字符串数组 | 会话 ID 属于列表 |
| sender | values: 字符串数组 | 发送者 ID 属于列表 |
| mentions_all | 无 | 数据库中的真实 @所有人 标记 |
| all | conditions: 规则数组 | 所有子条件成立 |
| any | conditions: 规则数组 | 任一子条件成立 |
| nearby | before、after、condition | 当前消息或指定邻居存在符合 condition 的消息 |

TOML 配置示例见 [context-filter.toml](examples/context-filter.toml)。把内容合并到已有 `[filters]`，不要替换账号、存储或调度配置。扩展规则 JSON 可通过现有配置 PATCH 和预览 API 传入 `filters.rules`。

## 新增种类的开放接口

可信本地 Python 扩展通过配置中已有的 `filters.code_file` 加载。实现 `register_filters(registry)`，调用 `registry.register(type_name, builder)`；无需修改微信或 QQ 适配器。种类名必须唯一，不能覆盖内置种类。

```python
from collector.filters import fields

def register_filters(registry):
    def text_length(rule, compiler):
        fields(rule, ("minimum",))
        minimum = rule["minimum"]
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ValueError("minimum must be a nonnegative integer")
        return "length(text) >= " + compiler.bind(minimum)
    registry.register("text_length", text_length)
```

规则可写为 `{"type":"text_length","minimum":20}`，也可放入 `nearby.condition`。完整示例见 [registered-filter.py](examples/registered-filter.py)。

builder 接收规则字典与编译器，校验参数后返回 SQL 表达式；值使用 `compiler.bind(value)` 绑定，组合条件使用 `compiler.compile(child)`。跨平台公共列为 `conversation_id`、`conversation_type`、`sender_id`、`sent_at`、`text`、`kind`、`mentions_all`。需要原生列的旧 `build_filter(settings) -> (sql, params)` 和 `sql_file` 仍保留，作为当前消息的基础限制；这类原生 SQL 不直接嵌入跨平台上下文条件。

扩展可同时定义旧 `build_filter` 和新注册函数。面板允许填写已注册种类的规则，不允许上传或编辑 Python 代码。扩展是可信本地代码，会在配置校验和执行时加载，注册函数不应产生外部副作用。

## 增量与去重

- 默认时间范围继续是 `[上次成功终点, 本轮开始)`。邻居可以来自范围外，但输出仅包含范围内的当前消息，不修改时间游标。
- 查询先用原生数据库取得本轮候选和同会话的边界邻居，再在临时 SQLite 表中用窗口函数判断；不会为此解码全历史消息。微信每个分库各取得足量边界邻居后统一计算。
- 同一消息被多个邻居或规则命中，只进入本次结果一次。最终仍按平台、账号、会话、消息 ID 去重，正文和附件不变就不产生新事件，变化则产生修订。规则名称或命中路径不写入消息语义载荷。
- 预览不调用附件归档或推进断点。输出消息的附件依旧遵循轻量／标准／完整设置。

## 风险与限制

尚未进入快照的后续消息无法参与判断。之后才出现的命中消息不会自动回补上一轮未输出的旧消息；要补查请重放相应时间段。这与原有严格时间增量规则一致，不新增隐式重叠窗口或待补队列。

上下文筛选比单条筛选需要更多读取和临时存储，大时间范围、多会话或较大的 N 会增加耗时。临时数据库含本轮消息，放在系统临时目录，正常结束会关闭并清理；进程被强杀可能遗留临时目录，应按聊天明文管理。没有新增 CPU/内存硬限额。

读取的是本地快照，不代表云端完整聊天顺序。没有进行全历史压力测试。注册器允许可信扩展执行代码，不是插件隔离机制；不要配置不信任的脚本。
