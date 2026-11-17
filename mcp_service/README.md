# Chat Collector Todo MCP

本地 stdio MCP 服务，复用 `processor` 和 `messaging` 的现有公开接口。它不启动新的计时器，也不绕过现有消息队列。

## 启动

```powershell
python -m mcp_service --root D:\opt\chat-collector
```

可单独指定配置：

```powershell
python -m mcp_service --root D:\opt\chat-collector `
  --processor-config config\processor.toml `
  --messaging-config config\messaging.toml
```

依赖：

```powershell
python -m pip install -r mcp_service\requirements.txt
```

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "chat-collector": {
      "command": "python",
      "args": ["-m", "mcp_service", "--root", "D:\\opt\\chat-collector"],
      "cwd": "D:\\opt\\chat-collector"
    }
  }
}
```

## 工具

- `tasks_search`：搜索待办。
- `tasks_get`：读取详情和当前 revision。
- `tasks_create`：复用现有审计创建接口。
- `tasks_set_status`：完成、恢复或取消。
- `tasks_set_priority`：修改 P0-P4，后续提醒按新优先级计算。
- `reminders_schedule`：截止节点、下一次提醒剩余秒数、目标计划和阻塞原因。
- `daily_digest_preview`：早报内容、排除项、投递状态和补发窗口。
- `notifications_configure`：带版本校验的截止提醒/早报配置修改。

## 边界

- 只监听本地 stdio，不开放网络端口。
- 不返回聊天原文、附件原始路径或内部数据库路径。
- 读取工具不会入队或发送消息。
- 修改配置只允许 `deadline_reminders` 和 `daily_digest` 两个白名单区段。
- 当前实现使用 MCP Python SDK 2.x 的 `MCPServer`；没有不存在的“MCP 2.0”协议概念。

规范参考：

- https://modelcontextprotocol.io/specification/2026-07-28
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- https://agentskills.io/specification
