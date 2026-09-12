# Code Mule（赛博码农）

[English](README.md)

> Powered by prompts. Paid in tokens.

基于 DeepSeek + Codex、由人类监督的自主软件工程系统。

把一个开发目标转化为受控的 **PLAN → CODE → REVIEW → VERIFY → COMMIT**
工作流。

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)
![Tests 719](https://img.shields.io/badge/tests-719%20passed-2E7D32)
![ProjectState v13](https://img.shields.io/badge/ProjectState-v13-6A5ACD)

## Demo

Code Mule 把目标推进为经过审查的本地 Git 交付：

```text
Boss Objective
  → PLAN → Task → Codex Worker → Supervisor Review
  → Verification → Local Git Commit → DONE

PROJECT
Status      Completed
Plan        v1
Progress    6 / 6

PROJECT COMPLETED
```

缺少产品决策时，当前 Task 会安全停止：

```text
ACTION REQUIRED
Category    Worker input
Question    排行榜应如何持久化？
Choices
- localStorage
- session memory

code-mule answer <action-id> "localStorage"
code-mule run
```

`answer` 只记录 Boss 的决定；下一次显式 `run` 会启动新的 Worker session，
继续同一个 Task。

## 为什么需要 Code Mule

常见 Agent 工作流仍由人类手工协调：

```text
Human → ChatGPT → copy prompt → Codex → copy result → ChatGPT
```

Code Mule 将它变成受控的工程流程：

```text
Boss → Supervisor → Runtime → Codex Worker → Review → Git Delivery
```

核心差异是：

- 使用持久化 `ProjectState`，而非隐藏的对话状态；
- 使用确定性编排，而非让自然语言直接控制流程；
- 使用 typed Human Gate 处理输入、批准和高风险操作；
- 使用 Git 原生、经过审查的 Task 交付。

## 工作方式

```text
Objective
    ↓
  PLAN
    ↓
Task → Codex Worker → REVIEW → VERIFY → COMMIT
 ↑                                      ↓
 └────────────── Next Task ─────────────┘
                       ↓
                 FINAL REVIEW
                       ↓
                      DONE
```

Supervisor 只提供 typed plan 和 decision。确定性 runtime 负责状态迁移、调度、
验证和 Git 交付。内部边界见[架构文档](docs/architecture.md)。

## 功能

- **自主规划** — 将一个目标拆成版本化 Plan 和具有依赖关系的 Tasks。
- **Codex 执行** — 每次由一个有边界的本地 Worker 执行一个 Task。
- **Supervisor 审查** — 每个 Task 必须经过审查才能交付。
- **Git 原生交付** — 每个被接受的 Task 形成一个精确的本地 commit。
- **需求变更** — CHANGE 经过影响分析，生成 Plan vN+1。
- **完成后继续变更** — 已完成的项目可接受新的 Boss CHANGE，并以新的线性
  Revision + Plan vN+1 继续，而之前的 Revision、Task 与 commit 保持不可变。
- **Human Gate** — Worker Input、批准和外部副作用会安全停下等待 Boss。
- **确定性恢复** — 持久化最近的执行边界、Safe Point 与 Worker attempt
  生命周期；`code-mule recover` 只在 `ProjectState` 能证明 Git 与 Plan
  连续性时继续。

## 人类控制

```text
CHANGE → Impact analysis → Replan
INPUT  → Boss answer → Fresh-session continuation
RISK   → Typed Human Gate
STOP   → Safe cancellation, no rollback
```

模型不能直接修改项目状态。存在歧义的副作用请求不会执行，批准也只绑定一个
明确 action。

## 前置条件

- Python 3.12
- Git，以及至少已有一个 commit 的目标仓库
- 已完成本地认证的 [Codex CLI](https://developers.openai.com/codex/cli)
- 用于 Supervisor 调用的 DeepSeek API key

安装后可用 `code-mule doctor` 检查以上所有项目，并明确提示缺什么。真实 E2E
已使用 **Codex CLI 0.153.4** 验证。这是实测版本，不代表已证明的最低支持
版本；`doctor` 不会把较低版本自动判为不兼容。

## 快速开始（Boss）

只需安装一次，之后都在你的目标仓库中工作。你不需要理解 virtualenv activation，
也不需要知道 Code Mule 源码或安装位置。

### 1. 一次性安装 Code Mule

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
bash scripts/install.sh
```

安装器会创建独立应用环境 `~/.local/share/code-mule/venv`，并在
`~/.local/bin/code-mule` 建立稳定 launcher。它不会安装到系统 Python，不依赖
仓库 `.venv`；当 `~/.local/bin` 不在 `PATH` 中时，会自动向正确的 shell rc
文件追加一段 Code Mule 管理的 PATH 配置，使新终端可以直接使用 `code-mule`。

关闭并重新打开终端：

```bash
command -v code-mule
code-mule --help
```

shell PATH 配置默认自动完成、幂等，并可通过 `--no-configure-shell` 显式跳过。
重复安装不会重复追加 PATH，也不会创建重复 backup；只有首次需要修改已有 rc
文件时才会创建一次备份（例如 `~/.zprofile.code-mule.bak`）。macOS 上安装器
优先写入登录 shell 环境文件 `~/.zprofile`，并为非登录交互 zsh 保留受保护的
`~/.zshrc` fallback。

### 2. 检查环境

```bash
code-mule doctor
```

每行都应为 `PASS`、`CONFIGURED` 或 `READY`。如果 DeepSeek 显示
`NOT CONFIGURED`，在 shell 中导出 key（不要提交它）：

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
```

可用 `CODE_MULE_DEEPSEEK_MODEL` 覆盖默认的 `deepseek-v4-flash`。真实
Supervisor 调用可能产生费用。

### 3. 在目标仓库启动项目

```bash
cd /path/to/my-project
code-mule start --objective "创建一个有测试的计算器"
```

`start` 以当前目录为 workspace、以目录名为项目名、以安全确定性的 slug 作为
project id，初始化 `.code-mule/project-state.json` 并立即执行该目标。

目标仓库必须是至少有一个初始 commit、且 working tree clean 的 Git 仓库。
新仓库可先建立最小 baseline：

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add README.md
git commit -m "chore: initial commit"
```

`start` 不会重新初始化已有项目，不会替你执行 `git init`，也永远不会 stash、
reset、clean 或替你创建 commit。被阻塞时会明确指出下一步。

### 4. 之后回到项目

在新终端中，从同一项目目录执行：

```bash
code-mule status
code-mule diagnose
code-mule recover
```

已完成的项目无需重新初始化即可继续演进：

```bash
code-mule change "排行榜刷新页面后仍然保留"
# 查看持久化的 ChangeRequest，然后
code-mule change --apply
```

随后 `status` 会显示新的 Revision 与 Plan 版本；历史 Revision 仍保留。

默认 state 文件是当前目录下的 `.code-mule/project-state.json`，因此命令会自动
发现项目。初始化会把 `.code-mule/` 登记到 repository-local Git exclude，不修改
tracked `.gitignore`，托管状态不会污染 Task baseline。`run` 和 `change --apply`
是阻塞命令；需要实时控制时可使用另一个终端。

在交互终端中，`status`、`diagnose`、`recover` 和 HumanAction 使用统一的
响应式 dashboard，并保留普通终端滚屏：

```text
┌────────────────────────────────────────────────────────────┐
│ CODE MULE · Calculator                                     │
├────────────────────────────────────────────────────────────┤
│ Status         Running                                     │
│ Plan           v2                                          │
│ Progress       █████████████░░░░░░░  4 / 6                │
│ Current        Implement leaderboard                       │
│ Safe Point     Task delivered                              │
└────────────────────────────────────────────────────────────┘
```

pipe、重定向、CI 和 `TERM=dumb` 环境仍输出稳定的纯文本，不带 ANSI 控制符。
支持 `NO_COLOR`，所有状态都同时有文字说明，不依赖颜色。

## 显式控制 / Contributor

需要显式命令的 Boss 也可以用相同方式初始化：

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
code-mule run --objective "创建一个有测试的计算器"
```

参与 Code Mule 源码开发时，使用仓库本地 editable 安装（不要把这种方式作为
普通 Boss 的 Quick Start）：

```bash
cd /path/to/code-mule
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/code-mule --help
```

开发预览命令示例：

```bash
.venv/bin/python scripts/preview_terminal.py --width 80
```

源码改动后刷新持久安装：

```bash
bash scripts/install.sh
```

安装器不会删除已有环境；默认再次执行仍会确保托管 PATH block 存在，若被移除
会自动补回。

## Boss 控制命令

| 命令 | 用途 |
| --- | --- |
| `doctor` | 只读检查 Code Mule、Python、Git、Codex、DeepSeek 与 workspace 就绪状态 |
| `start --objective "..."` | 用默认值初始化当前 Git workspace 并执行首个目标 |
| `run --objective "..."` / `run` | 开始规划，或继续 RUNNING 工作 |
| `status` | 读取确定性的 Plan 和 Task 进度 |
| `deliverable` | 查看已验证的交付物和使用方式 |
| `launch` / `app-status` / `stop-app` | 启动、查看并安全停止已验证的本地应用 |
| `diagnose` | 只读解释阻塞原因、可恢复性和 Boss 下一步操作 |
| `recover` | 从已持久化且通过确定性校验的执行边界继续 |
| `chat` | 打开自然语言 Boss 界面 |
| `ask "..."` | 查询项目，只读 |
| `change "..."` / `change --apply` | 记录 CHANGE，再显式执行 replanning |
| `pause` / `resume` | 在合法控制边界暂停或恢复 |
| `stop` | 安全取消，不回滚已完成工作 |
| `inspect` | 查看 pending typed HumanAction |
| `approve ID` / `reject ID` | 决定一个明确的 approval action |
| `answer ID "..."` | 记录一个 Worker Input 的答案 |
| `resolve ID --strategy ...` | 处理 typed 非 approval action |

HumanAction 命令只作用于指定 action。`answer` 不启动 Worker；它记录答案并保留
partial work，随后显式 `run` 会重新验证 baseline，用新 session 继续同一 Task。
详见 [Human Resolution](docs/human-resolution.md)。

## 适用场景

Code Mule 适合需要以下能力的本地软件开发：

- 多 Task 功能实现；
- 可审查、每个 Task 一个 commit 的 Git 历史；
- 需要人类决定产品选项的开发流程；
- 执行期间可能变化的需求；
- 跨 Worker session 持久化项目状态；
- 交付前进行本地验证。

## 可靠性与安全

- `ProjectState` 是持久化事实来源。
- 同一时间只有一个 execution owner 派发一个 Worker。
- inactivity timeout 与 hard turn timeout 分别有界。
- 中断或结果不确定时 fail-closed，不会静默重跑。
- Typed HumanAction 保留具体决策边界。
- 通过验证和审查后，仅 stage 当前 Task 确切拥有的路径。
- Code Mule 不会自动 stash、reset 或 clean 目标仓库。
- push、tag、release、deployment 和不可逆远端操作仍需 Human Gate。

详细说明：[Execution Recovery](docs/execution-recovery.md)、
[Git Delivery](docs/git-delivery.md)、[Project Verification](docs/project-verification.md)。

## 验证

当前 v0.1.1 发布准备基线：

- **719 项自动化测试 PASS**，包括 runtime handoff、PID 复用防护及不激活仓库 `.venv` 的真实 shell 重启回归测试；
- `compileall`、`pip check` 和 `git diff --check` PASS；
- 真实 DeepSeek + Codex E2E：Plan v1、6/6 Tasks、Worker Input → Boss answer
  → fresh-session continuation、每个 Task 一个 delivery commit、最终 review
  APPROVE、Git workspace clean；
- 真实 E2E 使用 Codex CLI **0.153.4** 验证。

该 disposable demo 中，未配置的项目级 test/lint/typecheck/build hooks 为
**SKIPPED**，optional browser visual verification 为 **NOT_RUN**——两者均未被
声明为 PASS。详细范围见 [v0.1.1 发布准备说明](docs/releases/v0.1.1.md)。
v0.1.1 GitHub Release 尚未创建；v0.1.0 tag 与历史证据保持不变。

## 当前限制

- 仅支持一个本地 Worker；不支持分布式或并行执行，也没有 daemon。
- 没有 Web UI。
- 被中断的 Codex turn 不会透明重连。
- 远端和不可逆副作用仍需 Human Gate。
- 项目验证只运行已配置 hooks，不会自行猜测缺失检查。
- 引入交付清单前的历史状态仍可读取，但不会根据推测数据启动。

## 文档

- [Architecture](docs/architecture.md)
- [Project Revisions](docs/project-revisions.md)
- [Boss CLI](docs/cli.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Runtime Handoff](docs/runtime-handoff.md)
- [Troubleshooting](docs/troubleshooting.md)
- [v0.1.1 Release Notes](docs/releases/v0.1.1.md)
