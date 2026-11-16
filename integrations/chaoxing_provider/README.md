# 学习通只读信息源

已针对 EmberCollective/loose-ends 上游 main `a4b970814eee97bd53a5e26961ae82fd90248856`（2026-11-15）适配。采集、SQL 兼容、筛选发布和 Pi 处理使用现有边界；不改变 QQ、微信、待办、投递、控制台或现有配置。导入本包不会启动服务、注册提供方或调用模型。

当前发布范围为明确授权的普通文字私聊/群聊。正式通知可在本地采集，但尚不导出；图片、文件、撤回/编辑及真实联系人姓名未实现。本模块不执行签到、作业或考试操作。

## 1. 初始化和会话引导

以下命令在仓库根目录执行，要求 Windows、Python 3.11+。常规采集只需标准库；浏览器引导另需 `pip install -r integrations/chaoxing_provider/requirements-bootstrap.txt`，无需下载浏览器。

```powershell
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture init
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture bootstrap --cdp-port 9222
```

先在自己的专用 Chrome/Edge 完成官方登录，将 9222 替换为该浏览器明确配置的本机 CDP 端口。引导只附加、读取学习通 Cookie，不导航、不关闭标签。也可 `import-session --cookie-file <明确提供的Cookie数组JSON>`；原文件由使用者保管，不自动删除。

验证 UID 后以 Windows DPAPI 加密保存，返回非凭据的 `account_id`（UID 的 SHA-256），并绑定采集库。同目录拒绝换账号；未知归属的非空旧库报 `unbound_history_use_new_state`，需新目录重新授权，不自动认领或清空历史。凭据不能搬到另一机器或 Windows 用户。Linux 凭据存储、密码重登和验证码自动处理未实现。常规后台采集无需浏览器常开，服务、网络、电脑仍需运行。

## 2. 授权并采集

```powershell
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture catalog
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture permit --source <来源ID> --enable
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture once
python -m integrations.chaoxing_provider.runtime --state-dir ./data/chaoxing/capture run --interval 60
```

来源默认关闭；`catalog` 只载入元数据。聊天名称未解析时显示匿名 ID，不猜老师/同学姓名。`permit --disable` 撤销，`--keyword` 增加采集侧精确文字条件。只收授权后聊天，正式通知首次为基线。包含断点、分页边界、账号核对、退避和跨进程锁；Ctrl+C 停止，无系统自启动项。

可信上层应用可调用 `runtime/api.py` 的 `status`、`catalog`、`permission`、`collect_once`，状态路径必须来自可信本地配置。它不启动 HTTP 服务。按上游外部提供方契约自行调度；默认控制台仍只管理 QQ/微信，没有新增学习通卡片或隐式调度。

## 3. 上游 SQL 筛选与标准发布

复制 `selection.example.toml` 为不提交的 `selection.local.toml`，设置：

- `source.account_id`：会话引导返回值，必须匹配采集库绑定账号。
- `source.source_ids`：明确已授权的会话 ID，不能为空表示全部。
- `source.database`：采集库；筛选状态和批次需使用独立位置。
- `filters.rulesets`：上游 SQL 与绑定参数，示例关键词为“作业”。

先明确一个较小时间窗，以实际 Unix 秒替换 START/END，左闭右开：

```powershell
python -m integrations.chaoxing_provider.selection_entry --config integrations/chaoxing_provider/selection.local.toml --start START --end END
```

默认仅打印数量，不输出正文、不推进状态、不发布。明确允许后添加两个参数：

```powershell
python -m integrations.chaoxing_provider.selection_entry --config integrations/chaoxing_provider/selection.local.toml --start START --end END --publish --allow-downstream-processing
```

批次目录须对应处理器配置。**已启用的处理/投递服务可能继续调用模型或发送通知**；此命令自身不调用这些服务。重复运行复用上游 `selection.Store` 的消息身份、修订、去重和 outbox 恢复。

SQL 映射位于 `providers/chaoxing.py`，经 `register_query_provider` 显式注册，筛选和处理核心不含学习通分支。来源事务只读，发布前复查快照，来源/权限改变需重试。窗口按捕获时间选择，`sent_at` 仍为真实发送时间，迟到消息可被下一窗发现；SQL 主动限制发送时间时可能排除这些消息。未知 sender_id 留空，不宣称已实现发送者筛选。

撤销权限阻止后续采集和新查询，不会撤回已发布或下游已消费的消息。不要同时对同一库使用旧桥接发布和新 SQL 发布。

## 验证与兼容

```powershell
python -m pytest tests/test_chaoxing.py integrations/chaoxing_provider -q
python -m pytest tests -q
python -m pytest tools/console/tests -q
```

专项均为合成输入，模型使用 stub；运行时测试包含 Windows 临时目录 ACL 和 DPAPI 往返，不读取真实账号。完整结果与未测边界见 [适配说明](../../docs/collector/compatibility/chaoxing.md)。合成通过不代表真实登录、新机器、付费模型或通知全链路验收。

旧 `adapter.py`、`__main__.py`、`config.example.json` 保留兼容：不执行上游 SQL，账号为人工别名、无法核验旧库归属。不推荐新部署使用，旧 ledger 不自动迁移，本机旧服务不被接管。
