# Code Mule（赛博码农）

[English](README.md)

> Powered by prompts. Paid in tokens.

> 你定义目标，牛马负责写代码。

Code Mule 是一个在明确人类控制下，完成规划、执行、审查、验证与任务交付的
自主软件工程 runtime。

## 为什么需要 Code Mule

传统 Agent 工作流仍由人类机械地协调：

```text
Human → ChatGPT → 复制 prompt → Codex → 复制结果 → ChatGPT
```

Code Mule 运行完整工程循环，同时让 Boss 保留最终权限：

```text
Boss → Supervisor → Runtime → Codex Worker → Review → Git Delivery → Verification
```

它的区别不是增加一个自由对话包装，而是使用持久 `ProjectState`、确定性编排、
结构化 Supervisor contract、明确 Human Gate，以及安全中断与恢复。

## 工作流

```text
Boss Objective
  → PLAN → Task Execution → Supervisor REVIEW → Verification → Git Commit
  → Next Task → Final Verification → DONE

CHANGE         → Safe Point → IMPACT_ANALYSIS → Plan vN+1 → Resume
STOP           → Safe Point → CANCELLED
HUMAN_REQUIRED → inspect → approve / reject / resolve
```

## 核心能力

- 结构化 DeepSeek `PLAN`、`REVIEW`、`IMPACT_ANALYSIS` 和 `FINAL_REVIEW`
  支持自主规划、多任务调度与版本化 replanning。
- 本地 Codex Worker 每次执行一个有边界的 Task；确定性 runtime 负责派发、
  状态迁移、验证和 Safe Point。
- Boss CLI 与自然语言 Chat 提供查询、CHANGE、PAUSE/RESUME、STOP 和绑定具体
  action 的 Human Resolution，不把状态权限交给模型。
- 单 execution owner、stale lease 检测、有界 Worker deadline 与 crash recovery
  guard 防止重复或结果不确定的 Worker 执行。
- 可重试的 Supervisor 输出结构错误使用有界完整重新生成；domain validation
  失败保持 fail-closed，不做模糊修复。
- 实时终端进度只投影安全的 Worker/Supervisor activity，不显示 prompt、推理、
  凭据或 raw model response。
- 仅精确 stage 并 commit Task 拥有的路径；Project 完成还要求配置检查、Git clean
  和 final review 全部通过。

## 架构

```text
Boss CLI / Chat
       ↓
Deterministic Orchestrator
       ↓
Supervisor ───── ProjectState
       ↓
Task Runtime
       ↓
Codex Worker
       ↓
Verification
       ↓
Git Delivery
```

`ProjectState` 是 source of truth，模型 conversation history 不是。Supervisor
只返回 typed proposal 和 decision，不直接修改状态或执行 Git 命令。

## 安全模型

自动允许：

- 本地 workspace 修改与已配置的本地验证；
- 使用 `git add -- <owned paths>` 精确 staging；
- 本地 Task commit。

必须经过 Human Gate：

- push、force-push、tag 和 release；
- deployment 与远端基础设施变更；
- 操作需要的 secret 或 API key；
- 付费外部操作；
- 破坏性或不可逆外部副作用。

高风险副作用不会通过解析模型自由文本获得授权。批准只绑定一个 typed
HumanAction，并且不可复用。

## 快速开始

### 1. 安装 Code Mule

克隆 Code Mule 工具仓库，并使用 Python 3.12 安装：

```bash
git clone https://github.com/Roxin-ChaI/code-mule.git /path/to/code-mule
cd /path/to/code-mule
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
which code-mule
code-mule --help
```

`/path/to/code-mule` 是 Code Mule 工具仓库，其中的 `.venv` 安装了 CLI。
切换到目标项目后仍需保持这个虚拟环境处于激活状态。每次打开新 Terminal，
都要重新执行 `source /path/to/code-mule/.venv/bin/activate`，也可以直接使用
绝对路径 `/path/to/code-mule/.venv/bin/code-mule`。

### 2. 准备目标项目 workspace

Code Mule 源码仓库只是工具本身；`--workspace` 指向 Code Mule 将要修改的目标
Git repository。两个目录的职责不同：

- `/path/to/code-mule`：Code Mule 工具及其虚拟环境。
- `/path/to/my-project`：Code Mule 实际修改的目标 Git workspace。

不要为了运行 Code Mule 而在目标 workspace 中创建 Code Mule 自己的 `.venv`。
如果目标项目本身需要 `.venv`，应由该项目自己的 `.gitignore` 管理。

目标仓库必须至少已有一个 commit，且 working tree 必须 clean。对于现有项目，
先进入其目录并检查基线：

```bash
cd /path/to/my-project
git status --short
```

只有当 `git status --short` 没有输出时才继续。对于新项目，先建立最小 Git
基线：

```bash
mkdir -p /path/to/my-project
cd /path/to/my-project
git init
printf "# My Project\n" > README.md
git add -- README.md
git commit -m "chore: initial commit"
```

clean baseline 用于准确识别每个 Task 自己产生的修改。Code Mule 不会自动
stash、reset，也不会覆盖无关的本地修改。

### 3. 初始化 Code Mule

必须在目标项目目录中执行初始化：

```bash
code-mule init --project-id my-project --name "My Project" --workspace "$PWD"
```

这里的 `$PWD` 是当前目标项目，不是 Code Mule 源码仓库。默认状态文件为
`.code-mule/project-state.json`。`.code-mule/` 是 Code Mule 的 runtime state；
`code-mule init` 会把这个受管理目录登记到 repository-local Git exclude，既不
破坏 clean baseline，也不修改项目中 tracked 的 `.gitignore`。

### 4. 配置 DeepSeek

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
```

不要把 API key 提交到目标项目或任何配置文件中。

### 5. 运行

使用目标描述启动真实 CLI 流程：

```bash
code-mule run --objective "创建一个有测试的计算器"
```

Code Mule 将依次推进：

`Objective → PLAN → Tasks → REVIEW → Git Commit → Final Verification → DONE`

### 6. 查看 / 控制

在目标项目目录中查看状态，或启动持续的 Boss 对话：

```bash
code-mule status
code-mule chat
```

例如，可以询问进度、提出变更或停止项目：

```text
You > 现在做到哪一步了？
You > 再加一个 JSON 导出功能。
You > 这个项目不做了。
```

命令默认发现 `.code-mule/project-state.json`。`run` 和 `change --apply` 会阻塞；
运行期间请从另一个终端执行控制命令。

### 故障排查

先确认 CLI 来自哪个安装位置：

```bash
which code-mule
```

预期路径为 `/path/to/code-mule/.venv/bin/code-mule`。如果没有输出该路径，
请重新激活 Code Mule 的虚拟环境。然后在 `/path/to/my-project` 中检查 Git：

```bash
git status --short
```

预期没有任何输出，包括刚执行完 `code-mule init` 时。普通 untracked、modified
或 staged 项目文件仍会正常显示，必须由用户明确处理；Code Mule 不会自动
stash、reset 或 clean 这些文件。

## Boss 控制命令

| 命令 | 用途 |
| --- | --- |
| `run --objective "..."` | 规划新项目；`run` 继续 RUNNING 工作 |
| `status` | 显示确定性的 Project 与 Task 状态 |
| `chat` | 启动自然语言 Boss 界面 |
| `ask "..."` | 只读项目查询 |
| `change "..."` | 记录需求变更 |
| `change --apply` | 执行影响分析、物化 Plan vN+1 并恢复运行 |
| `pause` / `resume` | 在控制边界暂停或安全恢复 |
| `stop` | 在 Safe Point 取消且不回滚 |
| `inspect` | 查看 pending HumanAction |
| `approve ID` / `reject ID` | 处理一个明确的 approval action |
| `answer ID "..."` | 回答一个明确的 Worker 输入请求 |
| `resolve ID --strategy ...` | 处理 typed 非 approval action |

命令加 `--verbose` 可查看 ID 与 raw control value；全局 `--debug` 显示经过清理的
traceback。

Worker 需要补充信息时，Code Mule 会停在 `HUMAN_REQUIRED`，`inspect` 会显示
经过边界限制的问题和选项。使用 `code-mule answer <action-id> "<answer>"`
回答该 action，再显式执行 `code-mule run`。Code Mule 会启动新的 Worker
session，先确认局部 workspace 修改仍与原始 clean Git baseline 一致，再继续同一
Task。`answer` 本身不会启动 Worker，已有局部修改会保留；它不会恢复已关闭的
Codex session，也不会为局部结果创建 commit。

人工操作按语义区分：`WORKER_INPUT` 是补充信息或产品选择，使用 `answer`；
`WORKER_APPROVAL` 和 `EXTERNAL_SIDE_EFFECT` 是对 `inspect` 展示的具体操作
进行审批，使用 `approve` / `reject`。批准只记录本次决定，不会自动执行该操作。
Worker 可以通过 Codex 原生输入请求，或在结束当前 turn 时返回 typed report
来提问；两者都复用上述 fresh Worker session 续接流程。

## CHANGE 示例

```text
You > 增加 JSON 导出功能。

Code Mule 记录 CHANGE，让当前 Task 到达 Safe Point，执行 IMPACT_ANALYSIS，
物化 Plan v2，并且只在显式执行 `change --apply` 后恢复。
```

## Boss Chat

`code-mule chat` 支持例如：

- “现在做到哪一步了？”
- “还有几个任务？”
- “当前有什么问题？”
- “再加一个 JSON 导出功能。”
- “这个项目不做了。”

只读事实来自 `ProjectState`；模型不估算进度，也不直接修改项目状态。存在歧义的
副作用请求会要求 Boss 澄清。

## 验证与交付

```text
Task:
Worker → verification → REVIEW → git add -- <owned paths> → local commit
       → COMPLETED

Project:
all Tasks complete → tests/lint/typecheck/build/git-clean → FINAL_REVIEW → DONE
```

只运行项目已配置的检查；没有配置的检查类别会跳过，而不会猜测命令。Task
`COMPLETED` 不代表 Project `DONE`。

## 可靠性

- 每个项目只允许一个 execution owner 派发工作。
- 恢复前会先分类 stale lease 与 interrupted Task。
- 每个 Worker turn 都有可配置的 inactivity timeout 和不可刷新的 hard maximum
  duration；只有当前 turn 的可信 activity 才刷新空闲时限。
- 任一 timeout 都会 fail-closed 进入 `HUMAN_REQUIRED`；局部修改不会被删除、
  reset 或自动重跑。
- 可重试的 Supervisor 结构错误进行有界全新生成，不做 JSON repair 或绕过
  validator。
- app-server process 与 reader thread 在成功、timeout 或 Ctrl+C 后都会关闭。

## v0.1.1 当前基线

发布准备基线：**498 项测试通过**，`compileall`、`pip check` 和
`git diff --check` 均 PASS。需要 Python 3.12。真实 E2E 使用
**Codex CLI 0.153.4** 验证；这是实测版本，不代表已证明的最低支持版本。

v0.1.1 修复了目标 Git baseline 的 state 隔离，支持安全重试 `WORKSPACE_BLOCK`、
Worker Input 回答后以新 session 续接、inactivity/hard 双重超时、安全 Codex
失败诊断和 typed Worker human actions。验证证据失败与 Git ownership 失败已
分开处理：required 检查必须 PASS；optional NOT_RUN 可接受，但 optional
FAIL/UNKNOWN 仍阻塞。精确路径归属、clean baseline、HEAD 与 staged-file
安全规则没有放宽。

当前 ProjectState 为 **schema v11**，保留 v1–v10 的历史迁移链。
新 state 可能无法被旧版 Code Mule 读取。升级前请备份 state；支持向前迁移
不等于支持降级或由旧程序读取新版数据。

最近一次 Boss 执行的真实 DeepSeek + Codex E2E 完成 Plan v1、**6/6 Tasks**，
每个 Task 恰好一个交付 commit，最终 Supervisor review APPROVE，Git workspace
clean。T4 通过 typed Worker report 请求输入；Boss 选择 `localStorage` 后，
显式 `run` 使用新的 Worker session 完成同一 Task。

验证范围需要区分：

- 项目级 test/lint/typecheck/build hooks 未配置，结果为 **SKIPPED**；
  required Git-clean 检查 PASS。
- Worker T6 独立记录自动测试 **20/20 PASS**、JavaScript 语法 PASS、
  `git diff --check` PASS。
- 浏览器视觉验证为 optional **NOT_RUN**，不代表人工浏览器验收通过；
  本次输入请求发生时没有 partial edits。

迁移、安全边界和验证详情见 [v0.1.1 release notes](docs/releases/v0.1.1.md)。
v0.1.0 tag 保持不变；本次准备不创建 v0.1.1 tag 或 release。

## v0.1.0 历史验证基线

以下是原 v0.1.0 release candidate 的历史数据，不是当前基线：

- Python 3.12.13 上 441 个 automated tests PASS；
- `code-mule==0.1.0` fresh editable install 与 `pip check` PASS；
- real local Codex full-system E2E PASS；
- real DeepSeek + real Codex release E2E PASS。

Authenticated release E2E 已验证：

```text
Objective → real PLAN → real Codex Worker → real REVIEW → CHANGE → Safe Point
→ real IMPACT_ANALYSIS → Plan v2 → Task commits → Project Verification
→ real FINAL_REVIEW → DONE
```

最终证据：Plan v2、3/3 Tasks completed、3 个 Task commits、3 个 unique Codex
sessions、verification PASS、`FINAL_REVIEW` APPROVE、workspace clean、execution
leases released，并且没有 duplicate Worker。

## 当前范围与限制

- 单个本地 Worker 和本机 execution ownership；不包含 distributed scheduler、
  daemon、parallel Worker 或 Web UI。
- interrupted Codex turn 不会透明重连；不确定工作需要检查并显式处理。
- push、tag、release、deployment 与其他 gated external effect 仍是手动 Human
  Gate 操作。
- verification command 是可信的确定性项目配置，不是 OS-level network sandbox。

## 文档

- [架构](docs/architecture.md)
- [Boss CLI](docs/cli.md)
- [Boss Chat](docs/boss-chat.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Project Cancellation](docs/project-cancellation.md)
- [Release Readiness](docs/release-readiness.md)
- [Troubleshooting](docs/troubleshooting.md)
