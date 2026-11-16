# 学习通只读来源：最新版适配

源码基线为上游 main `a4b970814eee97bd53a5e26961ae82fd90248856`，适配日期 2026-11-16。保留独立采集运行时，新增 SQL 提供方和显式应用组装入口。部署步骤见 [模块操作说明](../../../integrations/chaoxing_provider/README.md)。

## 职责与改动

| 层 | 位置与行为 |
| --- | --- |
| 采集 | `integrations/chaoxing_provider/runtime`：官方会话引导、DPAPI、授权轮询、断点、退避和原始 SQLite |
| 兼容 | `providers/chaoxing.py`：只读事务、授权/账号校验、文字标准消息、快照身份与查询注册 |
| 应用组装 | `integrations/chaoxing_provider/selection_entry.py`：显式注册；默认数量预览，双参数确认后才发布 |
| 筛选发布 | 复用 `selection` 的真实 SQL 命中、修订、去重、断点和 outbox |
| 处理投递 | 复用 `processor` / `messaging`，不加平台私有分支 |

原版本仅通过桥接导出事件，没有 SQL 注册。本版补充这一入口和账号绑定，拒绝未知历史自动归属。无需提交本机配置、聊天、凭据或 n8n 代码。内置来源、控制台和调度行为未变；控制台卡片仍只有 QQ/微信，这是 [外部提供方现有边界](provider-api.md#平台事实与调度边界)，未宣称学习通已有完整 UI。

按捕获时间划分窗口，发送时间保留为 `sent_at`；授权前发送、基线、未授权会话和未解析图片/文件不发布。SQL 侧只核验采集库的账号哈希，不读取 Cookie。尚无真实姓名/原生发送者映射、附件准备、正式通知导出、Linux 保险库或自动验证码。

## 回归依据

所有测试使用合成数据或本机 mock，未刷新真实消息库、调用真实模型或发送通知。

- `tests/test_chaoxing.py`：SQL 命中、捕获窗口/迟到消息、权限和账号拒绝、快照变化、重复与重启、标准事件及处理器兼容、默认预览与拒绝隐式发布。
- `integrations/chaoxing_provider/test_adapter.py`：旧桥接、上游契约、NTFS 发布、冲突和重试。
- `integrations/chaoxing_provider/test_runtime.py`：隔离导入、默认拒绝、合成协议、账号变化、锁及 DPAPI。

### 2026-11-16 本机结果

环境：Windows x64、Python 3.12、Node 24.20.0；独立虚拟环境，Pi 使用仓库锁文件安装，未改上游依赖声明。交付前再次 fetch 确认 main 仍为上述基线。

| 检查 | 结果 |
| --- | --- |
| `python -m pytest tests/test_chaoxing.py integrations/chaoxing_provider -q` | 23 passed |
| `python -m pytest tests integrations/chaoxing_provider -q` | 790 passed、24 skipped、4 failed，包含专项 |
| `python -m pytest tools/console/tests -q` | 116 passed、6 skipped |
| `node --test tests/runtime.test.mjs`（在 `processor/pi-runtime`） | 18 passed |
| `git diff --check` | 通过 |

完整套件**尚未全绿**，以下 4 项在无任何学习通改动的 main `a4b9708` 独立 worktree 中也同样失败：

1. `tests/test_automation_control.py::test_real_hidden_service_starts_restarts_stops_and_rejects_duplicate`：重复启动返回 PID 与初始回执不同。
2. `tests/test_installation.py::test_release_payload_never_copies_accounts_keys_or_installed_node`：纯源码检出缺少未纳入 Git 的 `runtime-deps/native/sqlcipher-pages.dll` 等发行组件。
3. `tests/test_qq_recovery.py::test_recovery_accepts_concurrent_wal_append_then_authenticates`：当前 Python 环境缺少 `sqlcipher3`。
4. `tests/test_qq_recovery.py::test_recovery_rejects_rewritten_wal_without_replacing_key`：同上。

最初还因缺少 Pi 依赖失败的 3 项核心及 2 项控制台测试，补齐上游锁文件依赖后已通过。未修改或删减原有测试来掩盖失败。因此本轮可确认专项和控制台通过、当前对照未见新增失败，不能宣称完整回归通过。真实登录、新机器运行、正式通知、发送者映射、付费模型/真实投递以及发行安装包尚未验收。此接入按源码方式运行，不扩展现有 Windows 发行包文件白名单。
