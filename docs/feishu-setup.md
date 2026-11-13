# 飞书应用机器人：待办、文件、按钮与长连接

本模块使用官方 `lark-oapi==1.7.5`。普通待办、按优先级的提醒、故障通知共用一个专用群。卡片按钮通过官方 WebSocket 回调进入本地反馈队列，处理层提交状态后再更新卡片；不需要公网服务或自行实现 WebSocket/OAuth。

## 1. 建应用、加机器人

在飞书开放平台创建企业自建应用，开启“机器人”能力。记录 **App ID / App Secret**；它们不是自定义机器人 Webhook 的地址或签名密钥。

把以下内容保存在本机 `.env` 或 Windows 用户环境变量中，不要发到群里、提交 Git 或填入公开配置：

```dotenv
CHAT_FEISHU_APP_ID=cli_实际值
CHAT_FEISHU_APP_SECRET=实际值
```

新进程才会继承更新后的系统环境变量。项目 `.env` 按次读取，无需把密钥复制到终端输出。安装独立消息依赖：

```powershell
python -m pip install -r messaging/requirements.txt
```

本机已把依赖装入 `runtime-deps` 的部署，可以先在当前终端设置 `$env:PYTHONPATH="$PWD/runtime-deps"`。

## 2. 批量导入应用权限

进入应用的“权限管理 → 批量导入/导出权限”，导入：

```json
{
  "scopes": {
    "tenant": [
      "im:message:send_as_bot",
      "im:resource",
      "im:chat:read",
      "im:chat.members:read"
    ],
    "user": []
  }
}
```

| 权限 | 用途 |
|---|---|
| `im:message:send_as_bot` | 应用发送卡片和文件消息；也覆盖更新本应用发送的卡片 |
| `im:resource` | 上传消息文件 |
| `im:chat:read` | 只读列出机器人所在的群，取得目标群 ID |
| `im:chat.members:read` | 只读列出指定群成员的姓名与 open_id，配置按钮操作者 |

本机 CLI 的新元数据列出了 `im:resource:upload`，但本次用户的开发者后台明确不接受该名称，所以使用稳定的 `im:resource`，**以实际后台可选权限为准**。不要导入 `im:resource:upload`。卡片更新元数据列出了 `im:message:update`、`im:message`、`im:message:send_as_bot` 三选一；这里已选最后一个，无需再申请全部消息权限。

这是应用身份权限，`user` 数组为空，不需要用户 OAuth。也不需要读取所有群消息、消息历史、通讯录或监听普通群聊天。如果后台不接受某个权限名，按同一 API 官方页面的权限候选与权限管理界面的名称核对，不应自行扩大到所有权限。

## 3. 配置回调长连接

先在本地配置中保留 `feishu-assistant` 目标和凭据环境变量名，然后运行连接检查：

```powershell
python -m messaging.feishu listen --setup --config config/messaging.toml --target feishu-assistant
```

`--setup` 仅建立官方长连接，永不发送任务、文件或修改待办。成功时仅输出 `status=connected, mode=setup`，不会输出凭据或带鉴权参数的连接地址。

在飞书开发者后台进入 **“事件与回调 → 回调配置”**：

1. 订阅方式选择 **“使用长连接接收回调”**，确认客户端已连接后保存。
2. 添加回调 **“卡片回传交互” `card.action.trigger`**。
3. 保存配置。卡片按钮属于“回调”，不是普通消息“事件订阅”；本项目不需要添加 `im.message.receive_v1`。
4. 在“版本管理与发布”创建版本并发布，按企业要求完成审核或管理员批准。发布范围包含实际使用者。

长连接不填公网请求地址。配置页若提示“应用未建立长连接”，先确认 setup 进程仍运行、系统环境已被该进程继承且网络允许出站 HTTPS/WebSocket。不要同时启动多个 setup/正式 listener。

## 4. 选专用群与操作者

在飞书建立或选择一个专用群，将应用机器人加入群。机器人只能操作自己可见且已加入的群。

以下命令都是**只读**，不发消息：

```powershell
python -m messaging.feishu list-chats --config config/messaging.toml --target feishu-assistant
python -m messaging.feishu list-members --config config/messaging.toml --target feishu-assistant --chat-id oc_实际群ID
```

将输出的群 `chat_id` 和自己的 `open_id` 填入目标：

```toml
[[targets]]
id = "feishu-assistant"
kind = "feishu_app"
enabled = false
receive_id_type = "chat_id"
recipient_id = "oc_实际群ID"
app_id_env = "CHAT_FEISHU_APP_ID"
app_secret_env = "CHAT_FEISHU_APP_SECRET"
allowed_operator_ids = ["ou_实际操作者ID"]
snooze_seconds = 3600
upload_files = true
max_file_bytes = 30000000
timeout_seconds = 30
```

只有白名单中的用户能点按钮；群里其他成员即使看见卡片也不能改待办。open_id 是当前应用范围内的用户身份，不应从另一个应用复制。群成员分页结果有 `complete=false` 表示受到企业安全配置限制，应调整权限或由管理员提供缺失操作者 ID。

`python -m messaging.feishu check --config config/messaging.toml` 只检查本地配置与 SDK，不检查网络和权限，不输出密钥。

## 5. 正式运行与停止

配置好后启用消息层全局 `enabled`、目标 `enabled` 和反馈配置，再停止 setup 并启动正式监听：

```powershell
python -m messaging.feishu listen --config config/messaging.toml --target feishu-assistant
```

正式 listener 只接收按钮并快速写入反馈队列，不在回调的几秒时限内解码数据库、调用模型或等待外部接口。看到“操作已接收”表示已排队，不代表处理层已经完成。处理层完成反馈后，常规消息投递刷新卡片。

周期执行、故障报告与隐藏进程由项目自动化入口管理；其报警目标填同一个 `feishu-assistant`。没有后台自动化时，单独启动 listener 不会自动扫描消息或发送提醒。

监听器支持 `--stop-file 路径`。管理进程创建该文件后，listener 关闭 SDK 连接并退出。SDK 1.7.5 尚无公共 stop，当前适配调用该固定版本的断开函数，不自行实现协议；SDK 首次获取连接地址时使用同步网络调用且未暴露超时，网络异常时停止可能延迟。管理进程应标记 stopping，避免强杀其他 Python 进程。

## 卡片、文件与防重复

- 卡片 2.0 展示执行步骤、截止日期、地点、参与人员、材料、来源与补充说明；无负责人或期限时只作说明，不进入人工校验门控，不展示内部 revision。
- 开放任务显示“完成待办”“稍后提醒”；已完成显示“恢复待办”；取消、替代和故障通知不提供任务修改按钮。
- 卡片更新后旧按钮失效；周期提醒发新卡片时旧卡片也失效。服务同时核对应用、目标群、消息 ID、操作者白名单、服务端保存的随机 token 和当前卡片代次。不会信任回传里自填的 task_id/action。
- 反馈携带卡片对应的任务 revision，避免队列延迟后旧“恢复”操作覆盖更新后的任务。同一按钮重复点击复用同一反馈事件 ID。
- 本应用自己的卡片在平台可更新时限内原位更新；超过保守的 13 天使用新卡片，避免依赖 14 天边界。取消/完成后本地保留历史与附件。
- 文件默认直接上传并作为独立文件消息发到同群，支持照片、PDF、DOCX 等通用文件。只读经过哈希核验的归档；空文件和大于 `max_file_bytes`（上限 30,000,000 字节）的文件明确失败。关闭 `upload_files` 时卡片明确提示仅本地保存。
- 每份文件按目标、待办和材料 ID 保存独立回执，成功文件不会随每次提醒重复发送。单个文件失败保留已成功卡片并记录 `files[].status/error`，**整体投递进入 retry 而不是成功**；按 `schedule.max_attempts` 有限重试，只补失败文件，不重发已成功卡片和文件。达到上限变为 failed，供自动化报警。结果未知的文件使整体进入 unknown，不自动重发；部分成功回执仍保存在消息层和飞书状态表，须先核对群里的实际结果。
- 创建消息使用稳定、最多 50 字符的 API `uuid`，并辅以本地持久记录；不宣称远程幂等窗口无限。网络超时、响应丢失或写入状态中断时保留 unknown，避免静默制造重复消息。

## 核对结果未知的投递

先查看群内实际卡片和文件，再对消息层显示的 delivery_id 明确处理：

```powershell
python -m messaging resolve-delivery --config config/messaging.toml --delivery-id 实际ID --resolution retry
python -m messaging resolve-delivery --config config/messaging.toml --delivery-id 实际ID --resolution delivered
```

两条命令择一执行。`retry` 允许重试本次未知部分，保留成功卡片、成功文件和已上传文件标识；发送沿用稳定 UUID，但平台远程去重窗口有限，必须先核对。`delivered` 表示人工确认卡片及本次文件已经送达，会同步解除飞书状态里的未知记录，不会再补发这些文件。当前应用和群必须与原投递一致，不能换群后确认旧记录。

如果未知卡片没有可信消息 ID，人工确认不能恢复其按钮：该卡片保存为已确认、撤销其按钮，不伪造消息 ID，也不撤销同任务更新卡片的按钮。未来任务更新或提醒会产生可操作的新卡片。已有可靠消息 ID 的成功卡片继续可用。

文件失败、未知结果和等待重试都属于未解决故障；即使后续一轮没有新消息，自动化也不会误报恢复，直到故障真正消除。反馈状态文件必须使用专用 SQLite，误指向待办或其他业务数据库时直接拒绝，不能混用。

## 依据与验收范围

- [官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
- [发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)
- [更新消息卡片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch)
- [上传文件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/file/create)
- [获取所在群列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat/list)
- [获取群成员](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-members/get)
- 卡片 schema、按钮 callback behaviors 与布局依据本机飞书技能所附 2.0 组件规范；发送、文件及群权限同时核对 CLI 元数据、参考文档与实际后台接受情况。

自动测试用真实 SDK 请求构造器与模拟响应，覆盖无权限/伪造回调、重复点击、旧卡片失效、更新、文件独立回执、未知结果、setup 无业务操作及停止信号。真实消息可达、文件下载和点击反馈仍需在用户提供群与操作白名单、明确启用后验收。
