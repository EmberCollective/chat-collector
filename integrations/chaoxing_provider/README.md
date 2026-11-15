# 学习通可合并模块：独立运行时 + 标准消息桥接

## 当前交付（第二步，2026-11-15）

新增runtime/，可把本目录放进对方仓库的integrations/chaoxing_provider/，从空目录运行，不再依赖原项目根目录、n8n、QQ/微信模块或原网页。仅Windows会话保险库，正常采集无第三方Python依赖；官方浏览器会话引导需要requirements-bootstrap.txt中的Playwright。不会下载浏览器、自动打开网页或扫描进程内存。

### 新机器设置顺序

在目标仓库根目录，先初始化私有状态目录（只修改你指定的新目录）：

```powershell
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state init
```

用户在自己专用的、仅本机开放调试端口的Chrome/Edge中完成官方登录，再明确导入该浏览器中的学习通Cookie：

```powershell
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state bootstrap --cdp-port 9222
```

端口是用户专用浏览器的实际端口，示例9222不是现有项目浏览器端口承诺。只附加，不导航、不关闭用户浏览器；仅读取三个学习通URL匹配的Cookie，不取其他网站Cookie。使用Playwright官方connect_over_cdp与context.cookies接口。也可import-session --cookie-file 自己明确提供的Cookie数组JSON文件；原文件由用户保管，本程序不自动删除。验证有效后仅DPAPI加密保存，不输出Cookie/令牌/密码。同一目录拒绝换账号，换账号必须新建独立状态目录。

```powershell
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state catalog
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state permit --source <目录返回的来源ID> --enable
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state once
python -m integrations.chaoxing_provider.runtime --state-dir D:/chaoxing-state run --interval 60
```

catalog只载入来源元数据；默认全关闭。permit需明确source，可用--disable撤销，--keyword添加文字条件。once只轮询已授权来源；run独立循环，不自动进入Windows启动项，Ctrl+C停止。私聊/群聊只收授权后新消息；正式通知首次为既有基线，不冒充新增提醒。账号变化会拒绝继续读，缺会话/过期时退出等待用户重新引导；限流和网络错误保存退避状态。采集周期有跨进程字节锁，不与另一实例重叠；权限修改可独立进行，版本核对丢弃撤销后的在途结果。

浏览器保存后常规REST采集无需浏览器常开；本机服务和网络仍需运行。DPAPI凭据不能搬到另一台机器或Windows用户。尚未支持Linux凭据存储、自动验证码、密码重登或全新机器的真实登录验收。

### 如何接对方调度与管理

可供其现有、已鉴权的控制台调用：

```python
from integrations.chaoxing_provider.runtime.api import status, catalog, permission, collect_once
# state_dir必须来自本地受信任配置，不能取HTTP请求里的任意磁盘路径。
# status(state_dir) / catalog(state_dir)
# permission(state_dir, {"id": selected_source, "enabled": True, "keyword": ""})
# collect_once(state_dir)
```

现有automation可定时调用collect_once；无授权来源时不发请求。CLI run是另一种运行方式，两者选一种管理，别同时设置重复调度。当前没有直接修改对方tools/console或automation代码，也没有伪装成已经安装的学习通卡片；需要在其目标分支中接这些固定函数。接口本身不启动HTTP服务器、不创建外部可访问的控制端口。

采集库为 STATE/classroom-output/school-reception/inbox.local.sqlite；用下方桥接CLI把它输出到上游contracts.ingest_batch，才进入对方处理管线。采集run本身不会发布给模型/通知。正式通知虽可在本地采集，首版桥接仍只输出普通文字私聊/群聊；图片、文件、老师姓名/会话名映射均未补齐，未知名称继续匿名，不从文本猜测。

### 验证和交付边界

在上游根目录运行：

```powershell
python -m unittest discover -s integrations/chaoxing_provider -t .
```

当前12项测试全通过：空目录隔离导入、默认不采集、合成文本捕获/去重、账号变化、并发锁、真实Windows目录设置和DPAPI往返，以及上游真实contracts.py校验与NTFS发布。所有数据合成，未使用现有账号再登录或在新机器验证；新运行时没有接管当前仍在运行的旧服务。

模块代码来自本项目已验证的只读接收/传输/协议/DPAPI层，改为包内相对导入。portable版目录不再查询课程名称，因此全部聊天名称可能显示匿名标识；用活动时间与明确来源ID定位。此处还不是普通用户的一键安装产品。

以下保留首版桥接的范围、配置、发布和失败恢复说明。运行时现已随包提供；首版章节中“依赖原采集服务”描述的是旧桥接的单独使用方式。

---

# 学习通 → chat-collector 提供方桥接（首版）

目标上游：EmberCollective/chat-collector，已核查提交 ebeb20a72d305d81211c3f76ed56b84c13001d20（2026-11-14）。

## 这是什么

这是实际可运行的Python代码，不是网页样式或n8n截图。放进上游项目的 integrations/chaoxing_provider/ 即可导入，无第三方Python依赖。通过上游公开 contracts.validate_event 和 contracts.ingest_batch 接入其 data/batches，不修改QQ、微信、processor、messaging或现有配置。

首版从现有学习通接收器已捕获的本地SQLite读取普通文字，转换成上游schema_version=1事件。原接收服务仍负责登录、REST补拉、授权、去重与时间边界。本目录**不是学习通完整独立采集器**；仅复制本目录不能在一台全新电脑上自动登录采集。首次交付选择小范围标准桥接，而非未经验证地复制全部旧模块。

## 支持与限制

- 必须明确配置source_ids，且来源当前仍已授权；不支持全部会话自动导出。
- 首版仅chat/singlechat/groupchat普通文字；正式通知、图片/文件、课程作业/考试任务暂不输出，未知消息不会伪装成正文。
- 按捕获时间[start,end)选取，排除基线与授权前发送的消息；sent_at转换为Unix秒，collected_at为真实捕获时刻，不拿发送时间代替。
- 账号标识是本地配置的稳定标识，不是认证。现有捕获库缺少账号分区：只支持单账号，换账号必须使用独立采集库及账号标识；本桥接不能核验平台账号归属。
- 原始消息ID已由现有采集器变成稳定本地哈希，保留此身份作为message_id，不冒充原生ID。sender_id留空，姓名和联系人身份明确未知，不把同学姓名写死进代码。
- matched_rules为空，不捏造上游SQL规则命中；新增SQL提供方尚未接入。因此上游是接收已选消息，不是自动把自己的SQL规则应用到学习通原始库。
- 外部提供方不会自动获得上游调试台的学习通卡片，也不自动加入其调度。对应UI/调度是下一层接入。
- 默认预览仅打印数量，不显示正文或凭据。写入上游batches可能让其已开启的模型/投递读取消息，发布须同时给出 --publish 和 --allow-downstream-processing。

## 配置和运行

将config.example.json复制为不提交的config.local.json，填入自己的绝对路径或相对配置目录的路径。capture_db指向已有接收器 classroom-output/school-reception/inbox.local.sqlite，source_ids来自该接收器状态；upstream_root指向已审阅的上游源码目录，inbox与上游处理配置一致，ledger置于自己的私有目录。不要使用示例时间窗直接跑历史；先选本次明确授权的短窗口。

在上游项目根目录运行：

```powershell
python -m integrations.chaoxing_provider --config integrations/chaoxing_provider/config.local.json
```

只有预览并明确允许下游处理后，再手动运行：

```powershell
python -m integrations.chaoxing_provider --config integrations/chaoxing_provider/config.local.json --publish --allow-downstream-processing
```

导入的contracts.py来自显式配置的可信本地上游源码，具备Python权限；不要指向陌生目录。本目录不包含对方contracts.py副本，沿用上游自己的实现与许可。

## 发布和失败恢复

自有outbox保存第一次生成的完整信封；重试沿用原时间窗，不因扫描窗口改变而生成冲突事件。上游成功发布后再标记完成。崩溃发生在发布之后、标记之前时，重试同一事件与同一文件，由上游幂等发布处理。只重试当前明确选中的事件，不顺带排空其他来源的旧待发项。已发布目标不应手动删除，否则需人工核对，不自动重发。

首版源记录按不可变处理：内容变化但仍使用原消息身份会报 capture_mutation_requires_revision_support，不伪造新事件或覆盖旧JSONL。修订和撤回迁移未实现。该桥接不承诺端到端恰好一次。

## 测试与验证

```powershell
python -m unittest integrations.chaoxing_provider.test_adapter
```

5项测试覆盖单源权限、窗口边界、基线、源库不改写、时间单位、未知姓名、稳定ID、重试恢复及内容冲突。上游真实校验/发布兼容测试需要独立取得并审阅的contracts.py，测试优先使用上游根目录的 contracts.py；在当前工程则参考 classroom-output/merge-reference/contracts.py；没有时明确skip，不能将skip当兼容通过。

本次在真实NTFS下5项全部通过，包含调用已审阅上游contracts.py的校验与真实JSONL发布；数据全部为合成消息，未把任何真实学习通消息发布给模型或外部通知。未运行上游完整processor/messaging，不能据此宣称全链路联调完成。

## 交付与下一步

本目录仅代码、合成测试、空值示例配置及说明；不含Cookie、DPAPI文件、真实账号ID、联系人、聊天数据库或真实批次。未向GitHub推送，也未代替作者选择项目许可证。

后续再将SchoolReception及登录会话层拆成独立运行时，明确新机器的凭据配置，再接上游调度/卡片。不要把当前项目根目录整个复制进上游，更不能整体打包data或classroom-output。
