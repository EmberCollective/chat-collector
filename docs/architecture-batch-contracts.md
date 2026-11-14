# 架构候选批次：接口冻结与实施记录

2026-11-13，用户已授权开始实施。只在独立 worktree 构建候选，不替换生产服务，不合主。

源码基线 `0d9fede`；此前未提交的已验证修复分别保存为 `c3b0307`、`e1dae9e`，架构设计保存为 `c0352b7`。生产目录的 README/图片和本机配置改动未纳入本批，不覆盖这些文件。

原生 Code Review / Security Review 尚未配置，用户已确认先完成候选实现。仅执行本地代码/安全检查，不能将其标为原生审查通过。所有子 Agent 统一 gpt-6.1-sol / high，包括返工总结；用户模型约束优先于流程中建议的其他模型。

## 冻结的调用约定

- 公共工具：`common.locking.InstanceLock`、`common.codec.canonical/digest`。旧 collector 导入仅作兼容。
- 收集：`collector.service.collect_once(config)` 独立刷新与进度，不加载规则。来源快照元数据由该服务返回，提供方读取时必须保证一致性；不得在模型工具中调用整轮采集。
- 查询：`providers.query.query_rules(scope, window, rules)`。scope 为 `{provider_id, platform, account_id, config}`，config 是本地收集配置对象，只在本地传递；window 为 `{start_inclusive,end_exclusive}`。rule 保留现有 `{file,sql,params,recent_count,provenance}`，增加稳定 `id`。返回 `{snapshot_id,messages:[{identity,message_key,message}],hits:[{identity,rule_id}]}`。筛选层负责 matched_rules 和事件发布。
- 筛选：`selection.run_round(config,start=None,end=None)`；规则加载/注册迁入 selection，兼容入口保留旧名称和语义。复合旧入口只作应用编排，收集实现不依赖 selection。
- 附件：继续复用 `providers.attachments.AttachmentContext`，通过 `providers.content` 公开工厂/内容入口访问。`read_attachment(ref,pages=None,query=None)` 与 `export_attachment(ref)` 使用当前批次绑定；底层结果保持模型无关。Pi 不导入 collector 或解析器。
- 待办：`processor.service` 提供 `get_task(config,task_id)`、`search_tasks(config,**filters)`、`create_tasks(config,tasks,*,request_id,actor,origin)`、`apply_extraction(...)`、现有状态/优先级接口、`read_changes(config,cursor=None,limit=100)`、`get_material(config,task_id,material_id)`。配置均是已解析处理配置；写事务不跨模型网络请求。apply_extraction 的分组参数由 A4 拆分现有保存段，保持来源校验和旧行为，不发明第二套模型提交 schema。
- 变更结果：`{events:[原有 task upsert 事件],next_cursor,has_more}`。cursor 为服务持有的 opaque JSON 值，消费者只持久存储与原样回传；首次为空，重放须稳定。优先使用现有 outbox 序号避免重新扫描全部历史目录。
- 材料结果：沿用已验证附件字典（name/type/archive_path/sha256/status 等），只能查询已绑定到任务的材料。消息层自己的成果附件仍由消息层管理。
- 反馈：保留 `enqueue_feedback(...)` 并新增可选 best-effort 唤醒；调用成功只表示入队。消息反馈通过 processor.service 写入，不直接连接处理库。
- 唤醒：`automation.wakeup.notify(config_path, job)`，有界、best-effort、无正文；调度宿主提供本机受控入口。具体实现由 A6 复用标准库，信号失效仍以持久队列/轮询恢复。信号不能绕过禁用、退避或未知投递保护。

接口细节如需改变，先报主 Agent并更新本文；不得各自实现冲突签名。小功能以独立提交交付，base/head diff additions+deletions ≤1000，超过必须说明原因，不机械拆成不可用的半功能。
