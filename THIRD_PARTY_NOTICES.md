# 第三方来源与许可

核验日期：2026-11-14。本文记录实际引入的代码、原生组件及主要许可约束，不替任何组件授予新的权利。精确版本、URL 与 SHA-256 见 [来源记录](third_party/provenance.json)；各目录现有的版权、许可与修改说明仍须保留。

## 当前授权缺口

仓库根目录目前没有项目 `LICENSE`。不能据此推定本项目为 MIT、GPL，或获准任意复制、修改、再分发。

项目级许可证建议、MIT 作为目标所需的前置条件，以及当前不可直接添加根许可证的原因，见 [许可与再分发](docs/guides/licensing.md)。

对外分发前须解决以下事项；附上本文件或免责声明不等于完成授权：

| 事项 | 已确认的事实 | 仍需完成 |
|---|---|---|
| 微信提取代码 | 官方对应版本未找到覆盖这些代码的项目许可证 | 取得权利人的明确授权，或移除并采用授权明确的实现 |
| 微信 `wx_key` | 固定 wheel 未附许可证；指向的源仓库对应标签也未找到项目许可证 | 核实二进制及其所含代码的授权链，取得许可或更换组件 |
| QQ 协议解析 | 直接引入 GPLv3 代码 | 评估组合程序的许可范围、对应源码与分发义务，并解决其他组件的兼容性 |
| PDF 解析 | PyMuPDF 2.1 明示 AGPLv3 / Artifex 商业双许可 | 选择并满足适用许可，核实组合程序与网络使用的义务 |
| 实际安装包 | 来源文件、完整许可证、版权通知须随实际材料保留 | 在最终包核验清单与原文齐备；本轮未发布安装包 |

GPL/AGPL 的义务不能仅靠把单个目录标为第三方来排除。取得微信授权、处理 GPL/AGPL 兼容性以及选择本项目许可证是不同事项，须一并解决；本文不作“已可合规公开分发”的结论。

## 微信离线核心

真实上游是 [LifeArchiveProject/WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)，不是仅凭名称猜测的其他项目。

- 官方 `v2.10.1` 在核验时指向 [`3169559943a13e8e5af014acfb61ef77ec657375`](https://github.com/LifeArchiveProject/WeChatDataAnalysis/tree/3169559943a13e8e5af014acfb61ef77ec657375)。`wechat_decrypt.py`、`sqlite_wal.py`、`sqlite_diagnostics.py`、`chat_helpers.py` 四个原文件将 LF 换行为 Windows CRLF 后，SHA-256 全部精确匹配本地 [提取时记录](collector/vendor/wechat/upstream-hashes.json)。这说明差异来自换行，而非另一个算法来源。
- 提取范围与后续修改保留在 [UPSTREAM.md](collector/vendor/wechat/UPSTREAM.md)，包括日志适配、增量 WAL、解压上限、时间索引与零填充尾页处理。未将修改后的代码称为上游原件。
- 已查看该固定提交的完整文件树、[README](https://github.com/LifeArchiveProject/WeChatDataAnalysis/blob/3169559943a13e8e5af014acfb61ef77ec657375/README.md) 及项目元数据，未找到覆盖这四个文件的项目许可证。树中的 `wcdb-key-tool-MIT.txt` 与 `WEFLOW_LICENSE.txt` 针对其他组件，不能套用于这些 Python 文件。
- 安装器使用的固定提交 [`22583491de87c574d26547e80ecbf9520419237f`](https://github.com/LifeArchiveProject/WeChatDataAnalysis/tree/22583491de87c574d26547e80ecbf9520419237f) 含相同的四个文件，亦未找到项目总许可证。公开可见、可下载或可 fork 均不代替再分发授权。

## 微信取钥 wheel

安装器锁定 `wx_key 2.0.1`，CPython 3.13 / Windows x64，下载位置与 SHA-256 记录在 [来源记录](third_party/provenance.json)。wheel 内的 `METADATA` 只有名称、版本与摘要，没有许可字段，也未附许可证文件。

WeChatDataAnalysis 的 [key_wheels/README](https://github.com/LifeArchiveProject/WeChatDataAnalysis/blob/22583491de87c574d26547e80ecbf9520419237f/tools/key_wheels/README.md) 指向 [H3CoF6/py_wx_key](https://github.com/H3CoF6/py_wx_key)。其 `v2.0.1` 在核验时指向 [`351c44af4dd3b0d21b92c94c842417357e88c47f`](https://github.com/H3CoF6/py_wx_key/tree/351c44af4dd3b0d21b92c94c842417357e88c47f)：完整树未找到项目许可证，`setup.py` 未声明许可，README 的研究用途免责声明不构成复制、修改或再分发许可。

这条链接确认了上游提供的出处线索，尚不能证明当前下载 wheel 可由该提交复现，或其内含代码均已获得授权。未从该仓库所参考的其他项目推导本 wheel 的许可。

## QQ 相关代码

| 组件 | 固定上游 | 许可与本地记录 |
|---|---|---|
| `c2c_40800_pb2.py`、`c2c_40800_parser.py`、`wire.py` | [QQBackup/nt_msg_db_util · `317024ce2c5e3482fdf9f310a5a7159ab4927e34`](https://github.com/QQBackup/nt_msg_db_util/tree/317024ce2c5e3482fdf9f310a5a7159ab4927e34) | 官方 [LICENSE](https://github.com/QQBackup/nt_msg_db_util/blob/317024ce2c5e3482fdf9f310a5a7159ab4927e34/LICENSE) 为 GNU GPLv3；本地 [原文](collector/adapters/qq_vendor/LICENSE) 与 [NOTICE](collector/adapters/qq_vendor/NOTICE.md) 已保留。三个文件核对一致，忽略换行差异。 |
| `ntqq_key_scan.py` | [q957506633/qq-nt-export · `bec1504170f99e2752793320711c5653f3803670`](https://github.com/q957506633/qq-nt-export/tree/bec1504170f99e2752793320711c5653f3803670) | MIT，`Copyright (c) 2026 qq-nt-export contributors`；本地 [LICENSE](tools/qq-nt-export/LICENSE) 与 [出处](tools/qq-nt-export/PROVENANCE.md) 已保留，扫描器 SHA-256 精确匹配。 |

QQ 扫描器的 MIT 不覆盖另一上游的 GPL 协议解析器。

## Pi 与界面资源

- `@earendil-works/pi-coding-agent`、`@earendil-works/pi-ai` 固定为 `1.5.2`。官方 npm 元数据声明 MIT；官方 `v1.5.2` 对应 [`b4afdddf44e059c67ccf5097e5317d913521b470`](https://github.com/earendil-works/pi/tree/b4afdddf44e059c67ccf5097e5317d913521b470)，npm 发布来源声明也指向该提交。本文读取了该声明，未独立验证其签名。保留 [MIT 原文](third_party/licenses/Pi-MIT.txt)：`Copyright (c) 2025 Mario Zechner`。
- `typebox 1.3.27`：保留已安装固定版本中的 [MIT 原文](third_party/licenses/TypeBox-MIT.txt)，`Copyright (c) 2017-2026 Haydn Paterson`。其他 npm 依赖仍分别适用自身许可；不得把 Pi 的 MIT 概括为所有传递依赖的许可。
- 调试台现有 Swagger UI 资源：本地已有 [Apache 2.0 原文](tools/console/static/vendor/LICENSE) 和 [NOTICE](tools/console/static/vendor/NOTICE)。本轮未重新核验其资产版本，不更改原通知。

## SQLCipher、SQLite 与 OpenSSL

| 材料 | 已核对来源与许可 | 保留材料 |
|---|---|---|
| SQLCipher 页访问 DLL 及 `sqlite3.c` | 构建使用固定 SHA-256 的 `sqlcipher3 0.6.5` 源码归档；归档内合并源码与当前本机源码 SHA-256 一致。源码标记 SQLCipher Community `4.20.0`、SQLite `3.54.1`，SQLCipher 部分为 BSD 三条款 | [原文件中的完整版权与许可块](third_party/licenses/SQLCipher-source-notices.txt)；构建参数及未修改标记见实际材料中的 `native/build.json` |
| `sqlcipher3` Python 包装层 | `sqlcipher3 0.6.5`，zlib 风格许可；该许可不替代其内 SQLCipher 代码的 BSD 条款 | [上游归档中的原文](third_party/licenses/sqlcipher3-Zlib.txt)；安装时还须保留包的 `.dist-info` |
| `dbdata.c` 及其 DLL | 固定 SQLite `3.51.0` 源码 ZIP 的 `ext/recover/dbdata.c`；源文件包含作者放弃版权的原始说明 | 保留源码原头部及 `native/dbdata-build.json`，参见 [SQLite 官方版权说明](https://www.sqlite.org/copyright.html) |
| `libcrypto-3-x64__.dll` | 当前本机二进制版本资源为 OpenSSL `3.6.1`，与 Strawberry 工具链原件 SHA-256 相同；工具链附带的许可原文与官方 `openssl-3.6.1` 一致，为 Apache 2.0 | [完整许可](third_party/licenses/OpenSSL-Apache-2.0.txt) 与 [版权及构建来源说明](third_party/licenses/OpenSSL-NOTICE.txt) |

这些是本次检查到的实际材料身份，不替代以后构建产物的校验。OpenSSL 当前只确认了工具链副本、版本资源与许可证文本，尚未核实该预编译 DLL 的完整源码及构建对应关系；不能把 SQLCipher 的“源码未修改”标记扩展到 OpenSSL。工具链提供的 OpenSSL 补丁记录仅写有 `Pack diff not generated`。

## PDF 与其他运行依赖

[安装锁](tools/install/requirements.lock.txt) 固定 `PyMuPDF 2.1`。该版本的 [官方 PyPI 元数据](https://pypi.org/pypi/PyMuPDF/2.1/json) 明示 `GNU AFFERO GPL 3.0 or Artifex Commercial License`，官方说明同时覆盖 PyMuPDF 与 MuPDF。保留从该版本源归档读取的 [AGPLv3 原文](third_party/licenses/PyMuPDF-AGPL-3.0.txt)。这不是本项目或其余组件的许可证，也不代表已取得商业授权。

`pymupdf4llm 2.1` 与 `pymupdf_layout 2.1` 的该版本官方说明为兼容升级所需的空包，相关功能已并入 PyMuPDF 2.x，不能套用旧版本或其他包的许可结论。

Python、Node、npm/Python 依赖及它们内含的库仍分别适用各自许可。安装或再分发时须保留随包的许可、版权、NOTICE 及元数据；若裁剪、合并或重新打包，需要重新检查实际产物。本次没有完成全部传递依赖、系统库、商标或平台协议的全面法律审查。
