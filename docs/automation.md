# 自动运行、重试和故障提醒

`automation/` 使用 APScheduler 3.11.2 调度现有模块，不复制采集、处理、投递或飞书回调的业务逻辑。它可以在没有调试台的情况下运行；配置在 `config/automation.toml`。

所有开关默认关闭。完成模型、目标位置、飞书应用和本地凭据配置后，按需开启全局 `enabled` 和对应任务。启用微信或 QQ 自动化之前，先关闭调试台对应的定时采集，避免两套调度交替执行。现有采集状态库锁仍会拒绝同一模块并发写入。

## 独立任务

| 配置项 | 执行内容 | 是否调用外部服务 |
|---|---|---|
| `jobs.wechat` / `jobs.qq` | 原有采集模块的一轮刷新、筛选和发布 | 由采集模块配置决定 |
| `jobs.processor` | 从已发布消息提取一批待办 | 调用配置的模型 |
| `jobs.messaging` | 导入待办、准备材料、同步和提醒 | 仅消息层启用的目标 |
| `jobs.feedback` | 消费已验签、已入队的用户操作 | 由反馈处理接口决定 |
| `listener` | 维护官方 SDK 飞书长连接进程 | 连接飞书接收事件 |

采集、处理、消息、反馈彼此独立调度。一项失败不会阻塞其他项。处理层只读已发布消息；不会因为收到新提醒而重新解密数据库。`pending_groups > 0` 表示仍有后续批次，本轮已经完成的部分属于成功。

各任务的 `interval_seconds` 是成功结束后到下一轮的间隔。`scheduler.control_interval_seconds` 控制恢复命令和到期任务的检查间隔，实际启动可能多等待这一间隔。APScheduler 的线程池、`max_instances=1`、`coalesce=true` 加上状态库原子认领防止同一个任务重叠执行；错过的周期合并为下一次，不补跑一串历史周期。

配置修改后重启自动化服务生效。面板保存时，运行中的后台会等待当前任务完成后重启；停止状态只保存，须明确点击启动。`resume` 无需重启服务。面板的“运行一轮”可以在该任务定时关闭时执行一次，不改变定时开关，仍遵守消息层投递开关与失败暂停状态；原 CLI `once` 只运行已启用任务。

## 有限重试

`[retry]` 给出默认值，每个任务及 `listener` 可单独覆盖：

| 参数 | 含义 |
|---|---|
| `max_attempts` | 包含首次执行的总尝试次数 |
| `backoff_seconds` | 首次失败后的等待时间 |
| `max_backoff_seconds` | 指数退避上限 |

等待时间为 `min(backoff_seconds × 2^(attempts-1), max_backoff_seconds)`。成功后次数归零，按普通间隔继续。失败耗尽后保存为 `paused`，后续普通周期和进程重启均不会自行重试；修复配置或故障后，明确执行 `resume`。

进程意外退出时，正在执行的任务会在下次启动计为一次已消耗尝试，以 `interrupted_execution` 进入退避或暂停。每个业务模块继续使用自己的事务、检查点和投递幂等机制；自动化不会声明跨进程、跨网络“恰好执行一次”。Pi 的模型请求重试、消息层的单项投递重试与自动化的整轮异常重试是不同边界。

消息或反馈函数返回 `failed/conflict/unknown > 0` 时记录 `downstream_attention`，继续处理其余数据，不对已经由下层管理的单项结果再套整轮失败重试。消息层还检查持久投递记录中的失败、未知和待重试数量；下一轮没有发送也不会误报恢复。需要人工对账的未知投递不会被自动化擅自恢复。

## 状态与报警

`data/automation/state.db` 独立保存每项任务的状态、尝试次数、下次运行时间、最近成功时间、数值结果和本地故障记录。它不会修改处理层或采集层数据库；配置误指到其他应用的已有数据库会拒绝启动。

同一任务、同一错误代码在 `alerts.cooldown_seconds` 内合并并累计出现次数。故障解除后可发送恢复通知，`alerts.recovery_enabled=false` 可关闭。SQLite 错误保留引擎错误码，例如 `sqlite_busy`，不再全部显示为 `OperationalError`。本地状态的 `service["last_failure:任务名"]` 保存该任务最后一次失败时间、项目内调用位置及错误链；恢复后仍保留，面板显示最近故障位置。记录不包含异常原文、SQL、局部变量、聊天正文、URL 或凭据；外部报警仍只发送错误码。

自动化状态库使用 SQLite WAL，让面板读取最近已提交状态时不阻塞后台写入。升级时仅迁移一次并保留任务、重试次数和设置；正常连接不再反复初始化表结构。备份运行中的状态库请使用 SQLite backup API，不要只复制 `.db` 而遗漏尚未合并的 WAL。

外部报警默认关闭。模板的 `alerts.target_id="feishu-assistant"` 与消息层的飞书应用目标对应；启用时同时配置该目标和消息层全局投递。通知使用 `kind=alert`，只有任务名称、故障代码及检查建议，没有业务待办操作按钮。报警投递自身失败只记为 `failed_or_unknown`，不递归发报警、不进入无限重试；下次满足冷却时间的同类故障才可能再通知。

## 命令与隐藏启动

先安装依赖；本机已有固定版本 APScheduler，可直接使用：

```powershell
python -m pip install -r automation/requirements.txt

# 只检查配置和组件，不刷新数据、不请求模型、不验证远端 API
python -m automation check --config config/automation.toml

python -m automation status --config config/automation.toml

# 只执行明确指定且已启用的任务；不默认运行全部任务
python -m automation once --config config/automation.toml --job processor

# 持续运行；Ctrl+C 等待正在执行的任务收尾
python -m automation run --config config/automation.toml

# 修复故障后恢复一个暂停任务
python -m automation resume --config config/automation.toml --job wechat

# 请求正在运行的服务退出，不杀其他 Python 进程
python -m automation stop --config config/automation.toml
```

本机也可使用 `tools/automation/start.cmd` / `stop.cmd`。启动器优先使用本机 Python，再使用系统 Python；后台进程用 `pythonw` 和隐藏窗口启动；标准输出不留存，运行状态、尝试次数和安全错误码保存在 `data/automation/state.db`，通过面板或 `status` 查看。启动器会加入本项目 `runtime-deps`；直接使用命令行运行 QQ 时，也需要正确安装或加载对应 SQLCipher 运行库。未安装开机启动项。

采集权限继承启动者。如果本机源数据库访问或解码需要管理员权限，应从管理员会话启动；隐藏窗口不会提高权限，自动化也不会自行弹窗提权。现有调试台定时任务不会被此启动器自动关闭，启用相同采集任务前需先停掉原调度。

同一自动化状态库只允许一个服务实例。停止请求等待正在执行的任务完成；不会在解密事务、模型调用或远端写入中途强杀它们。

## 飞书长连接生命周期

调试台新增自动化与通道管理入口，提供后台服务启停、各任务间隔、提醒频率、通道实例配置、运行状态和合成测试。配置保存使用版本检查；新增目标可单独启停，密钥仍只存本机环境变量或 `.env`。面板关闭不停止独立后台服务。公共管理接口与测试记录见 [通道配置与合成测试](messaging-management.md)。

飞书首次配置完成后，先停止 `listen --setup` 连接检查进程，再启用消息层全局开关、目标和自动化的 listener，最后启动后台服务。后台从已启用的 `feishu_app` 目标选择应用，目标可以使用自定义 ID；同一应用下其他群目标按已保存的卡片 token 自动路由，并分别核对群与操作者。自动监听只支持一个应用，启用的目标属于不同应用时会明确拒绝启动并记录 `feishu_listener_multiple_apps_not_supported`；需要多应用时，关闭自动化 listener，为各应用单独启动 `python -m messaging.feishu listen --config config/messaging.toml --target 目标ID`。不要为同一应用的多个群重复启动连接。

“发送合成测试”会实际发送明确标注的测试内容，只有已经启用的目标可用。测试按钮只写本地测试点击记录并提示成功，不创建、完成、取消或恢复正式待办。测试卡片到达、附件可打开和回调记录成功应分别检查；发送成功不能代替回调验收。

长连接使用现有 `python -m messaging.feishu listen --config ... --stop-file ...`，协议和验签由飞书模块处理。首次配置需要的 `listen --setup` 操作独立执行，自动化不会替用户进入配置模式或打开投递开关。

监听器进程的 PID、创建时间和完整固定命令会保存。自动化进程重启后核对并接管原监听器，避免重复启动；在启动与记录 PID 之间发生崩溃时，也按相同模块、配置和停止标记寻找原进程。如果找到多个匹配进程则暂停并报警，不再新增监听器。

监听进程退出后使用有限退避重启；普通健康轮询不会清空重启预算。恢复通知表示进程重新启动，不等于证明飞书网络握手已经成功。重启耗尽后使用 `--job listener` 恢复。

停止时写入停止标记，等待官方 SDK 监听器退出，最多等待 10 秒。SDK 初始化网络请求阻塞时可能延迟响应；自动化保留 `stopping` 状态，不强杀进程。再次启动时会先处理仍存活的旧监听器。正常停止不删除待办、材料、回调队列或任何失败记录。

## 验证范围

`tests/test_automation.py` 使用合成任务和时钟验证有限重试、暂停持久化、手动恢复、同任务并发拒绝、故障隔离、报警去重、不递归报警、监听器接管和重启预算；另有真实 APScheduler 调度合成任务的测试。测试不请求真实模型、不读取实际聊天、不发送飞书消息。`check` 通过也不代表真实权限、网络或远端配置已经验证。
