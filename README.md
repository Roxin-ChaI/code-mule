# Code Mule

> Powered by prompts. Paid in tokens.

> You define the goal. The mule does the coding.

中文名：赛博码农（牛马）

Code Mule is a human-supervised autonomous software development system.

## Problem

Current agentic coding workflows often require a human to perform mechanical coordination:

- copying prompts from a Supervisor to Codex;
- copying execution reports from Codex back to the Supervisor;
- advancing development phases;
- maintaining the current project status; and
- replanning after new or changed requirements.

Code Mule aims to remove this coordination burden while preserving human authority over important decisions.

## Goal

The Boss is responsible only for:

- defining goals;
- querying the project;
- changing requirements;
- making key decisions; and
- approving Human Gates.

The system is responsible for:

- planning;
- task decomposition;
- execution dispatch;
- execution review;
- verification;
- progress tracking;
- change impact analysis; and
- replanning.

## v0.1.0 Scope

The v0.1.0 MVP defines a single-project, single-worker workflow coordinated by a deterministic Orchestrator. It uses structured contracts, versioned plans, durable project state, safe interruption, verification, and fail-closed Human Gates. It is an MVP definition, not a production-ready system.

## Architecture

```text
Boss
  ↕
Supervisor
  ↕
Orchestrator
  ├── Project State Store
  └── Codex Worker
```

## Local Setup

Use the repository-local Python 3.12 environment so Code Mule and its declared runtime dependencies share one interpreter:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Do not rely on packages installed in the global `python3.12` environment.

## Manual DeepSeek E2E

This command makes a real DeepSeek API request and may incur charges. It is a Boss-only manual gate and is not run by automated verification or Codex.

```bash
export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_deepseek_supervisor_e2e.py
```

See [DeepSeek Supervisor Provider](docs/deepseek-supervisor.md) for the runtime and state boundaries.

## Local Codex Worker

Phase 6 provides a Python library boundary for executing one task through the
locally installed `codex app-server`. It uses the executable's stdio JSONL
protocol and contains no API key or token configuration. Approval and user-input
requests fail closed; Code Mule does not answer or approve them automatically.

See [Codex Worker](docs/codex-worker.md) for the tested Codex version, lifecycle,
sandbox boundary, and conservative `ExecutionReport` mapping. This is not yet a
CLI or an autonomous Supervisor–Orchestrator–Worker loop.

## Status

Early development.

The DeepSeek Supervisor provider and local Codex Worker boundaries are in v0.1.0 development. The Supervisor uses the official `openai` Python SDK only as an OpenAI-compatible client for the DeepSeek endpoint, while the Worker uses the local Codex app-server process. ProjectState remains the Source of Truth. Automated tests do not call real model APIs, and real DeepSeek E2E remains a Boss-only manual gate. Autonomous orchestration and production readiness are not yet available.
