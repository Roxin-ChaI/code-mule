# Code Mule（赛博码农）

[English](README.md)

> Powered by prompts. Paid in tokens.

基于 DeepSeek + Codex、由人类监督的自主软件工程系统。

把一个开发目标转化为受控的 **PLAN → CODE → REVIEW → VERIFY → COMMIT**
工作流。

![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)
![Tests 511](https://img.shields.io/badge/tests-511%20passed-2E7D32)
![ProjectState v11](https://img.shields.io/badge/ProjectState-v11-6A5ACD)

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
- **Human Gate** — Worker Input、批准和外部副作用会安全停下等待 Boss。
- **持久化恢复** — 从 `ProjectState` 恢复，而不是依赖聊天记录。

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

开始前检查本地 Codex：

```bash
codex --version
codex app-server --help
```

真实 E2E 已使用 **Codex CLI 0.153.4** 验证。这是实测版本，不代表已证明的
最低支持版本。

## 快速开始

### 1. 安装 Code Mule

将工具安装在独立目录中：

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
code-mule --help
```

`/path/to/code-mule` 是工具仓库。保持这个虚拟环境处于激活状态，或直接运行
`/path/to/code-mule/.venv/bin/code-mule`。

### 2. 准备目标仓库

`--workspace` 指向另一个 Git 仓库，也就是 Code Mule 实际修改的项目：

```bash
cd /path/to/my-project
git status --short
```

目标仓库必须至少有一个 commit，且上面的命令必须没有输出。新项目可以先建立
最小 baseline：

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add -- README.md
git commit -m "chore: initial commit"
```

clean baseline 用于识别每个 Task 自己产生的修改。Code Mule 不会自动 stash、
reset、clean 或覆盖无关本地修改。

### 3. 初始化 workspace

请在目标仓库中执行，而不是在 Code Mule 源码仓库中执行：

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
```

默认 state 位于 `.code-mule/project-state.json`。初始化会把 `.code-mule/`
登记到 repository-local Git exclude，不修改 tracked `.gitignore`，因此托管状态
不会污染 Task baseline。

### 4. 配置 DeepSeek

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
```

不要提交 API key。真实 Supervisor 调用可能产生费用。

### 5. 执行目标

```bash
code-mule run --objective "创建一个有测试的计算器"
```

### 6. 查看与控制

```bash
code-mule status
code-mule chat
```

相关命令会从目标仓库发现 `.code-mule/project-state.json`。`run` 和
`change --apply` 是阻塞命令；需要实时控制时可使用另一个终端。

## Boss 控制命令

| 命令 | 用途 |
| --- | --- |
| `run --objective "..."` / `run` | 开始规划，或继续 RUNNING 工作 |
| `status` | 读取确定性的 Plan 和 Task 进度 |
| `diagnose` | 只读解释阻塞原因、可恢复性和 Boss 下一步操作 |
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

- **498 项自动化测试 PASS**；
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
- 已完成项目目前不能通过 CHANGE 重新打开；DONE 不会迁移到
  CHANGE_REQUESTED。
- 项目验证只运行已配置 hooks，不会自行猜测缺失检查。

## 文档

- [Architecture](docs/architecture.md)
- [Boss CLI](docs/cli.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Troubleshooting](docs/troubleshooting.md)
- [v0.1.1 Release Notes](docs/releases/v0.1.1.md)
