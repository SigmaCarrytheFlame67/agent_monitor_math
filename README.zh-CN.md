<p align="center"><img src="docs/public/readme/ansatze-icon.svg" width="60" alt="Ansätze" /></p>
<h1 align="center">Ansätze</h1>
<p align="center"><strong>面向数学研究、证明探索与证据审查的一体化工作空间。</strong></p>
<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong> · <a href="README.fr.md">Français</a></p>
<p align="center">
  <a href="https://ansatze.ai/"><img src="docs/public/readme/ansatze-icon.svg" width="17" alt="" /> ansatze.ai</a> &nbsp;·&nbsp;
  <a href="https://github.com/ericjiang18/agent_monitor_math"><img src="docs/public/readme/github-icon.svg" width="17" alt="" /> GitHub 源码</a> &nbsp;·&nbsp;
  <a href="https://ansatze.ai/quickstart">使用指南</a> &nbsp;·&nbsp;
  <a href="CONTRIBUTING.zh-CN.md">参与贡献</a>
</p>
<p align="center"><a href="https://github.com/ericjiang18/agent_monitor_math/actions/workflows/ci.yml"><img src="https://github.com/ericjiang18/agent_monitor_math/actions/workflows/ci.yml/badge.svg?branch=main" alt="main 分支的 Source checks 状态" /></a></p>

Ansätze 将 **Continual Graph Memory（CGM，持续图记忆）**、数学智能体监控、文稿分析与 Lean 验证证据整合在同一个研究控制台中。从问题陈述出发，逐步查看计划、证明尝试、验证器反馈和保存的结果；通过相应的图和产物，判断每一项结论的依据。

本仓库包含 **[ansatze.ai](https://ansatze.ai/)** 的网站代码、网站使用的有界 CGM 执行流程，以及**完整的独立 CGM 研究框架**。两种实现具有各自的运行环境与配置。

> **区分不同层次的证据。** 模型接受某个证明步骤，只代表模型的判断。证明大纲、搜索图或通过编译的命题声明都不能单独确立一个定理。Lean 编译、公理检查，以及形式化命题是否忠实对应原问题的检查会分别报告。

<p align="center"><img src="docs/public/readme/shared-explore.png" width="1080" alt="Ansätze 只读分享界面的实际浏览器截图：控制台导航、Explore 与 Facts 标签，以及问题、计划、子目标和证明尝试组成的图。" /></p>
<p align="center"><em>截图来自实际分享页面，展示的是人工构造的 CGM 回归测试数据。生成此示例未调用模型或检查器；它用于演示界面，并非研究结果。</em></p>

**导航：** [功能](#功能) · [从这里开始](#从这里开始) · [界面说明](#界面说明) · [本地安装](#本地安装) · [独立 CGM 框架](#独立-cgm-框架) · [架构](#架构) · [公开贡献](#公开贡献) · [合并与部署](#从拉取请求到网站更新)

## 功能

| | 可以做什么 | 可以查看什么 |
| --- | --- | --- |
| 🧠 **持续图记忆** | 协调策略规划、证明工作器、独立模型验证器、有界修复与整理器。 | 已接受事实的依赖关系，以及含义不同的完整探索记录。 |
| 📝 **证明工作空间** | 输入自己的问题或选择公开问题，阅读并继续受支持的运行。 | 排版后的证明、产物、参考文献和尚未解决的缺口。 |
| 🔎 **文稿分析** | 读取受支持的链接、文本或文件，对结果进行分段和检查。 | 文稿图、局部判断、依赖关系汇总与分析报告。 |
| 📊 **智能体监控** | 跟踪受支持证明引擎的执行过程。 | 阶段活动、智能体摘要、记录的 token 用量与费用，以及保存的产物。 |
| 🧩 **Lean 集成** | 通过配置好的 Lean 工具与沙箱代理，形式化和检查产物。 | Lean 源码、编译结果、公理信息，以及独立的命题对应性证据。 |
| 🔗 **只读分享** | 发布、更新或撤销一个明确保存的运行快照。 | Sandbox、Monitor、Lean 证据，以及可用的 Outline、Explore、Facts、Formal、Compiler facts 或 Manuscript 图。 |
| 🗂️ **研究网站** | 浏览公开问题集、来源引用和研究图谱。 | 数据来源、带日期的状态依据及原始文献链接。 |

具体功能取决于账户权限、服务商路由、引擎安装情况和运行保存的产物。缺失的检查不会被当成检查成功。

## 从这里开始

| 目标 | 入口 |
| --- | --- |
| 体验托管控制台 | 打开 [ansatze.ai](https://ansatze.ai/)，参阅[快速入门](https://ansatze.ai/quickstart)。访客访问取决于托管容量与策略。 |
| 了解界面 | 阅读下方的[界面说明](#界面说明)，再查看[完整使用指南](docs/quickstart.md)。 |
| 本地运行控制台 | 按照[本地安装](#本地安装)操作。 |
| 开展持久化的自主 CGM 研究 | 使用[独立框架指南](cgm-framework/README.md)。 |
| 改进项目 | 阅读[公开贡献](#公开贡献)及[贡献指南](CONTRIBUTING.zh-CN.md)。 |
| 运维托管服务 | 阅读[部署指南](deploy/ansatze/README.md)、[自动更新](deploy/ansatze/AUTO_DEPLOY.md)与[运维文档](OPERATIONS.md)。 |

本说明提供中文导航与解释。控制台中的功能名称保留英文，便于与实际界面对应；部分深入文档目前为英文。

## 界面说明

控制台有四个主要视图：**Sandbox** 展示证明或分析报告；**Monitor** 解释记录的执行过程；**Lean Verification** 展示形式化源码与已保存的检查；**DAG** 从不同角度呈现数学论证及其产生过程。

| DAG 视图 | 含义 |
| --- | --- |
| **Explore** | 搜索过程：目标、计划、尝试、验证器反馈与整理记录。这里的边不一定代表逻辑蕴含。 |
| **Facts** | 通过所配置模型验证流程的陈述及其记录的依赖关系。这种接受不等于 Lean 证书。 |
| **Outline** | 证明结构的非形式化大纲。 |
| **Formal · Lean** | 已保存的形式化证明图（若存在）。 |
| **Compiler facts** | 从所配置 Lean 编译器环境提取的依赖关系，需要有有效的已保存证据。 |
| **Manuscript** | 被分析文稿中的结果和引用，以及记录的模型判断。 |

以上浏览器截图使用人工构造的测试数据。分享模式中的所有视图均为**只读**，只描述已保存的快照。访问者不能启动模型调用或形式化检查。旧分享链接可能不包含创建时尚未保存的图；所有者需选择 **Create / update snapshot**，将目前可用的视图加入快照。所有者可以撤销链接，但无法收回读者已经保存的副本。

## 本地安装

### 1. 安装控制台

使用 **Python 3.11–3.13**；主控制台的 CI 使用 Python 3.13。主要部署环境为 Linux。外部智能体与 Lean 服务有额外依赖。

```bash
git clone https://github.com/ericjiang18/agent_monitor_math.git
cd agent_monitor_math
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
agent-monitor create-admin
./start.sh
```

打开 **http://127.0.0.1:4600**，使用刚创建的本地管理员账户登录。创建命令会交互式询问凭据；不要把密码放入命令行参数。如果端口已被占用，运行 `AGENT_MONITOR_PORT=4601 ./start.sh`。

### 2. 配置模型服务商

在 Git 忽略的 `.env` 中设置需要使用的服务商。支持的路由和限制见 [.env.example](.env.example)。启动服务器本身不需要付费模型调用；启动实际证明或分析可能产生服务商费用。

如需通过界面保存每个账户的服务商设置，请在**启动服务器之前**配置本地加密密钥：

```bash
mkdir -p .secrets
python - <<'PY'
import os
from pathlib import Path
from cryptography.fernet import Fernet
key = Path('.secrets/local-settings.key')
if not key.exists():
    with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(Fernet.generate_key())
PY
export AGENT_MONITOR_DATA_KEY_FILE="$PWD/.secrets/local-settings.key"
./start.sh
```

先停止原来的前台服务器，再重新启动。请将此密钥与私有本地状态一并妥善保管：丢失或更换它后，无法解密用它保存的设置。`.env`、`.secrets/` 及运行状态不纳入 Git。默认状态目录为 `data/`，也可通过 `AGENT_MONITOR_DATA_DIR` 指定其他目录。

完整的[安装脚本](setup.sh)可能安装可选工具和下载上游引擎，运行前请阅读。外部编码智能体 CLI 需要单独认证，控制台的账户与执行策略仍然适用。[外部源码说明](engines/EXTERNAL_CHECKOUTS.md)列出了未随仓库打包的来源。

### 3. 运行公开网站

使用与网站 CI 一致的 Node.js **22** 和 npm。在开发副本的仓库根目录运行：

```bash
npm ci
npm run docs:dev
```

VitePress 会输出本地开发地址。如需预览生产构建：

```bash
npm run docs:build
npm run docs:preview
```

文档网站与 Python 控制台是两个独立进程。本地网站预览不会自动配置账户、模型路由、Lean 代理或生产环境的反向代理。

## 独立 CGM 框架

完整框架位于 **[cgm-framework/](cgm-framework/README.md)**，包含持久化事实图与探索图、按角色划分的 MCP 服务、任务编排、验证、策略咨询、执行后端、智能体契约与技能、示例和测试。

| 网站 CGM | 独立框架 |
| --- | --- |
| 在控制台任务中运行的有界、仅返回响应的流程。 | 通过独立智能体与服务管理持久化研究项目。 |
| 使用控制台账户、服务商路由、取消机制和预算控制。 | 使用自身的配置、启动后端、角色契约和操作流程。 |
| 随主 Python 包安装。 | 从 `cgm-framework/` 目录单独安装。 |

查看框架并运行测试：

```bash
cd cgm-framework
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m cgm.orchestration --help
python -m pytest cgm/ -q
```

框架支持 Python 3.10+，并提供无需模型调用即可创建和查看项目状态的[离线示例](cgm-framework/README.md#inspect-the-code-without-a-model-account)。实际启动工作器前，请阅读[入门指南](cgm-framework/docs/getting-started.md)、[配置](cgm-framework/docs/configuration.md)与[安全和信任边界](cgm-framework/docs/security-and-trust.md)。运行网站不需要安装独立框架。

## 架构

![概念架构：问题或文稿进入控制台，有界 CGM 与 Analyze 生成保存的产物，视图分别展示不同类型的证据。](docs/public/readme/architecture.svg)

控制台认证请求并应用账户策略，随后为每次运行创建工作空间、选择执行器。网站 CGM 协调证明尝试，记录已接受事实和探索状态；其他适配器将各自引擎转换为统一运行模型。监控层汇集事件、产物、用量和图。Analyze 分析现有文稿，配置好的 Lean 服务则单独检查形式化产物。

| 路径 | 职责 |
| --- | --- |
| [agent_monitor/](agent_monitor/) | 控制台 API、账户、任务、模型路由、分析、分享与 Lean 集成。 |
| [agent_monitor/cgm/](agent_monitor/cgm/) | 网站 CGM：策略规划、工作器、模型验证器、修复与整理器。 |
| [agent_monitor/web/](agent_monitor/web/) | 交互式控制台与只读分享界面。 |
| [cgm-framework/](cgm-framework/README.md) | 完整的独立 CGM 项目与文档。 |
| [monitor_core/](monitor_core/) | 事件和产物处理、依赖图与监控视图。 |
| [docs/](docs/) | VitePress 网站、Vue 组件、公开目录、研究图谱与媒体。 |
| [engines/](engines/) | 随附引擎源码和集成说明；可选引擎可能需要单独安装。 |
| [deploy/](deploy/) | Caddy、systemd、自动更新、托管模型网关与沙箱 Lean 代理。 |
| [tests/](tests/)、[evals/](evals/README.md)、[scripts/](scripts/) | 回归测试、评测输入、目录构建与维护工具。 |
| [problems/](problems/) | 控制台使用的问题清单与源问题。 |

版本控制只包含公开代码、测试、配置模板和必需的公开资源。私有运行、账户数据库、上传文件、服务商凭据、生成的评测结果和依赖安装目录不纳入源码。公开目录的来源说明见[问题来源](docs/problem-sources.md)。

## 开发检查

安装控制台开发依赖后，在根目录运行：

```bash
python -m pytest tests/ -q
node --test tests/*.mjs
npm run docs:build
```

独立 CGM 测试需在其子目录单独运行。测试使用本地样例和模拟服务商；部分测试会启动回环服务器或子进程。[Source checks 工作流](.github/workflows/ci.yml)检查控制台、框架和网站。软件测试通过并不意味着数学结论已获验证。

## 公开贡献

欢迎使用 **English、简体中文或 Français** 参与。可贡献的方向包括文档、无障碍交互、回归测试、服务商集成、图语义、文稿分析、可复现实例和独立框架。

- **第一次贡献：**完善不清楚的说明、补充小型人工测试样例，或修复键盘操作与移动端问题。
- **报告缺陷：**说明受影响视图、复现步骤、预期行为和运行环境，并提供脱敏截图或最小示例。
- **提出功能：**描述研究任务及界面应展示的证据；较大改动请先通过 issue 讨论。
- **提交拉取请求：**在 fork 和分支上开发，提供针对性的检查；界面改动附上前后截图，并同步翻译和来源说明。

先阅读 **[贡献指南](CONTRIBUTING.zh-CN.md)** · [English](CONTRIBUTING.md) · [Français](CONTRIBUTING.fr.md)，然后[创建 issue](https://github.com/ericjiang18/agent_monitor_math/issues/new/choose)或[查看拉取请求](https://github.com/ericjiang18/agent_monitor_math/pulls)。不要在公开 issue 或截图中包含密钥、访问令牌、私有分享链接或用户文稿。

## 从拉取请求到网站更新

```text
拉取请求 → Source checks → 代码审查 → 合并到 main
                                         ↓
                             main 上的 Source checks 通过
                                         ↓
                              部署定时任务 → 健康检查
                                         ↓
                                   ansatze.ai 更新
```

[自动部署机制](deploy/ansatze/AUTO_DEPLOY.md)需要先在生产服务器上完成**一次性安装**。启用后，它监视公开仓库，只有所有必需的 Source checks 任务成功，才部署对应的 `main` 提交。部署包含健康检查和回滚，不需要 GitHub 个人访问令牌或 GitHub Actions SSH 密钥。

审查批准不会自动合并分支；**合并到 `main` 才是发布触发点**。检查失败时，线上版本保持不变。部署指南说明了启用、状态查看和恢复方法。请在开发副本中构建：直接修改线上源码或重新构建其网站可能立即影响生产服务。

## 常见问题

<details>
<summary><strong>绿色事实节点意味着定理已经形式化证明了吗？</strong></summary>

不是。Facts 记录的是所配置模型验证器的接受结果。Lean 编译与命题对应性是另外的证据，需要分别理解其范围。Analyze 中通过编译的命题声明并不包含证明。

</details>

<details>
<summary><strong>为什么分享页面缺少某个图？</strong></summary>

快照可能创建于该视图推出之前，或运行从未生成对应产物。所有者可更新快照，加入目前已有的数据。分享模式不会为了补齐缺失结果而启动模型或检查器。

</details>

<details>
<summary><strong>需要安装所有可选证明引擎吗？</strong></summary>

不需要。配置你要使用的引擎和服务商路由即可。仓库包含适配器，不代表外部程序、环境或凭据已经安装。独立 CGM 项目使用自己的安装流程。

</details>

<details>
<summary><strong>可以不用私有运行或付费调用测试界面吗？</strong></summary>

可以，使用回归测试和人工构造的样例即可。本说明中的截图就是这样生成的。贡献内容应排除私有账户数据和实际可访问的私有分享链接。

</details>

## 来源与许可

组件来源和条款见 [ATTRIBUTION.md](ATTRIBUTION.md)。独立框架保留 [Apache 2.0 许可](cgm-framework/LICENSE)；网站改编版本包含[许可](agent_monitor/cgm/LICENSE)与[修改说明](agent_monitor/cgm/NOTICE.md)。随附引擎、库、字体和公开数据各自保留原有条款，不能用一个仓库徽章替代这些许可。

README 图片来源及 GitHub 图标说明见[资源说明](docs/public/readme/README.md)。GitHub 标志仅用于标识源码链接，不表示 GitHub 对项目的认可。
