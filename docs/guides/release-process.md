# 发布流程

本流程适用于 Windows x64 本地使用预览包的版本发布。发布是独立动作：代码合并、版本号变更或 `CHANGELOG.md` 更新都不代表已经发布。

> **硬门禁：** 只有用户明确同意目标版本、tag 和发布范围后，才执行打 tag 与 GitHub Release。未获同意时停在已合并的提交，不推送 tag，不创建 Release，也不对外宣称该版本可用。

公开再分发前还必须阅读 [许可与再分发](licensing.md)。当前项目存在无明确许可和 GPL/AGPL 组件，不能把“可以构建 ZIP”当作已经取得公开再分发许可，也不能宣称整体为 MIT。

## 1. 冻结发布范围

1. 确认要发布的功能已通过独立评审并合入 `main`；目标提交的 GitHub Actions CI 必须通过。不要把未评审实验、个人筛选规则或本机配置带入发布。
2. 在干净工作区同步远端：

   ```powershell
   git fetch origin
   git switch main
   git pull --ff-only
   git status --short
   ```

   `git status --short` 应为空。工作区有改动时先逐个确认归属，不能直接提交或覆盖。

3. 核对目标 tag 尚不存在：

   ```powershell
   git ls-remote --tags origin vX.Y.Z
   ```

   已在远端存在的版本不改 tag、不覆盖资产；有问题时发布新的补丁版本。

## 2. 核对版本文档

1. [version.json](../../version.json) 是唯一版本来源。版本使用 `X.Y.Z`，调试台、安装产物和发布清单都读取该值。
2. [CHANGELOG.md](../../CHANGELOG.md) 顶层版本必须与 `version.json` 一致，并包含日期和用户能理解的新增、修复、更改与限制。
3. 确认 README 仍明确标注实验性质、机型与客户端兼容性风险，且许可说明与 [第三方说明](../../THIRD_PARTY_NOTICES.md) 一致。
4. 版本和说明通过普通主题 PR 合入；不要在已经构建产物后临时修改版本号。

至少验证统一元数据：

```powershell
python -m pytest tests/test_version_metadata.py -q
```

## 3. 完整验证

以下 Python 与 Node 指项目固定运行环境；准备方式见 [开发指南](development.md)。先确认运行环境版本和依赖，不要把“命令返回 0”代替真实安装验收。

```powershell
$env:PYTHONPATH = "$PWD/runtime-deps"
python -m pytest tests -q

Push-Location tools/console
python -m pytest tests -q
Pop-Location

Push-Location processor/pi-runtime
npm test
Pop-Location
```

涉及调试台时启动只使用合成数据的服务：

```powershell
python tools/console/tests/ui_fixture.py --port 18447
```

另开终端运行受影响页面的验收；发布至少覆盖概览和运行监控，并补跑本次触及的页面：

```powershell
$env:CONSOLE_TEST_URL = "http://127.0.0.1:18447"
node tools/console/tests/verify-ui.cjs
node tools/console/tests/verify-overview.cjs
node tools/console/tests/verify-runtime-monitor.cjs
```

按改动补跑 `verify-automation.cjs`、`verify-delivery.cjs`、`verify-processor-tasks.cjs` 等脚本。验收服务必须通过 `/__fixture__` 合成标记，不连接真实微信/QQ、不调用模型、不向外部渠道发送消息。

CI 不包含未提交的原生 SQLCipher 包和发布产物断言，因此只能作为基础门禁；发布验收仍须在具备实际原生包的 Windows x64 环境运行完整测试。

发布验收记录至少写明：执行日期、源码提交、依赖版本、Windows 版本、通过项、失败项和未测项。真实客户端取钥、新账号首次处理、网络投递或特定机型结论未实测时，不得写成已通过。

## 4. 构建与产物审计

从干净提交构建：

```powershell
python tools/install/build_release.py
```

默认产物为 `dist/chat-collector-windows-x64-preview.zip` 和同名 `.zip.sha256`。上传前将 ZIP 重命名为版本化资产 `chat-collector-X.Y.Z-windows-x64-preview.zip`，并保留对应校验文件。

检查包内 `release-manifest.json`：

- `version` 与 `version.json` 完全一致。
- `source_commit` 等于 `git rev-parse HEAD`。
- `working_tree_dirty` 为 `false`。
- 文件清单包含源码、默认模板和第三方许可材料，不包含 `config/*.toml`、`tools/console/console.toml`、`.env`、`secrets/`、`data/`、`.runtime/`、日志、真实聊天或附件。
- `THIRD_PARTY_NOTICES.md`、`third_party/` 中的许可证与来源记录随包交付；README 和文档中的相对许可链接在解压后仍可打开。

对解压后的产物再做一次隐私与授权检查：搜索个人账号、`wxid_`、飞书 ID、用户名目录、令牌、密钥和本机绝对路径；人工查看新增资源与截图。发现任何真实凭据时停止发布；已经推送或上传过的凭据必须视为泄露并更换。

记录最终校验值：

```powershell
Get-FileHash dist/chat-collector-X.Y.Z-windows-x64-preview.zip -Algorithm SHA256
```

校验值必须与 `.zip.sha256` 一致。包内许可缺口见 [第三方说明](../../THIRD_PARTY_NOTICES.md)；没有完成授权判断时，不得扩大分发范围或改为 MIT 声明。

## 5. 打 tag 与创建 Release

只有硬门禁通过后执行。tag 必须指向实际验收的提交：

```powershell
git tag -a vX.Y.Z -m "拾序 · Loose Ends vX.Y.Z"
git push origin vX.Y.Z
```

创建 GitHub Release 时：

- 标题使用 `拾序 · Loose Ends X.Y.Z`，发布说明只摘取 `CHANGELOG.md` 中该版本内容，不把开发日志或未验证能力写成用户已获得的功能。
- 上传版本化 ZIP 与 `.sha256`；资产上传后不再静默覆盖。
- 明确写明 Windows x64 实验性预览、未验证范围、备份要求，以及微信/QQ 客户端更新可能导致取钥、解密、增量刷新或采集失效。
- 若许可条件仍未解决，必须保留对应限制说明，不得使用“MIT”“开源可自由再分发”等未经确认的表述。

## 6. 发布后复验与本地同步

1. 从 Release 链接重新下载 ZIP 和校验文件，按上一步的 SHA-256 重新核对。
2. 在空目录安装下载得到的包，确认包内版本、安装结果、重复启动、停止和调试台读取一致；已有部署不得被覆盖。
3. 同步本机调试台时使用同一发布产物和版本号。先备份 `.env`、`config/`、`secrets/` 与 `data/`，在独立目录安装并验收，再切换本地入口；不要把这些本机文件打进包或提交。
4. 记录实际验证的机器、Windows 与客户端版本，以及仍未验证的兼容性。不能把单机通过写成“任意机型可用”。

## 7. 失败与回滚

- tag 前失败：修复问题、重新验证、重新构建；不得复用旧产物。
- tag 后发现问题：保留历史 tag 和 Release 事实，发布新的补丁版本并在说明中写明影响；不要移动已发布 tag 或悄悄替换 ZIP。
- 涉及隐私或凭据泄露：立即停止分发、移除暴露载体、轮换凭据，再评估新的发布版本。