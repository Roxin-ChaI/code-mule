# Code Mule（赛博码农）

> 你定义目标，牛马负责写代码。

Code Mule v0.1.0 是一个由人类监督、确定性编排的本地软件开发系统。
Boss 保留目标、需求变更、关键决策和 Human Gate 的最终权限；Supervisor
只负责结构化推理，Orchestrator 控制状态机，Codex Worker 执行任务，
ProjectState schema v8 是唯一持久化事实来源。

## 本地安装

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
CODE_MULE_BIN="$(pwd)/.venv/bin/code-mule"
```

## 从零开始

```bash
mkdir -p /tmp/code-mule-calculator
cd /tmp/code-mule-calculator
git init
git config user.name "Code Mule Boss"
git config user.email "boss@example.invalid"
touch README.md
git add -- README.md
git commit -m "chore: initialize workspace"

"$CODE_MULE_BIN" init \
  --project-id calculator \
  --name "Calculator" \
  --workspace "$PWD"

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
# 终端 A（阻塞执行）
"$CODE_MULE_BIN" run --objective "创建一个有测试的计算器"

# 执行期间，在同一目录的终端 B 中运行
"$CODE_MULE_BIN" status
"$CODE_MULE_BIN" chat
"$CODE_MULE_BIN" change "增加 multiply 功能"

# 终端 A 到达 CHANGE Safe Point 后
"$CODE_MULE_BIN" change --apply

# 或者取消仍在执行的项目
"$CODE_MULE_BIN" stop
```

默认状态文件是当前目录下的 `.code-mule/project-state.json`，因此后续命令
会自动发现项目；只有使用非默认位置时才需要 `--state-file`。执行前 workspace
必须是拥有初始 commit 的 clean Git repository。
`run` 是阻塞命令；控制命令应由另一个终端读取同一个持久化状态。`stop`
是取消路径，不是在 Project 已经 DONE 后执行的步骤。

## 安全边界

- 自动允许：本地文件修改、本地测试、精确路径 `git add`、本地 commit。
- 必须人工批准：push、force-push、tag、release、部署、付费调用、secret
  使用、破坏性删除/迁移以及不可逆外部副作用。
- `resume` 不会绕过 `HUMAN_REQUIRED`；批准只绑定一个具体 HumanAction，
  且不可复用。
- STOP 在 Safe Point 取消项目，保留历史与已完成 commit，不 reset/revert。

## 文档

- [CLI](docs/cli.md)
- [Boss Chat](docs/boss-chat.md)
- [Human Resolution](docs/human-resolution.md)
- [Execution Recovery](docs/execution-recovery.md)
- [Git Delivery](docs/git-delivery.md)
- [Project Verification](docs/project-verification.md)
- [Project Cancellation](docs/project-cancellation.md)
- [Release Readiness](docs/release-readiness.md)
- [Troubleshooting](docs/troubleshooting.md)

自动化测试和本地发布 E2E 不调用 DeepSeek。完整真实 DeepSeek + Codex
发布链仍由 Boss 手动执行 `scripts/manual_release_e2e.py`，该流程会产生真实
API 请求和费用。v0.1.0 是单项目、单 Worker 的本地 MVP，不包含 daemon、
并行 Worker、Web GUI 或自动 push/tag/release。
