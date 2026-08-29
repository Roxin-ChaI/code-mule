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

## Status

Early development.
