# Codex Worker

Phase 6 adds a local execution boundary between Code Mule and Codex. It is a
library integration, not an autonomous orchestration loop or a CLI.

## Tested Runtime

The implementation was developed against `codex-cli 0.147.0`. Protocol fields
were checked from the installed executable with:

```bash
codex app-server generate-json-schema --out /tmp/codex-app-server-schema
```

The generated schema is intentionally not copied into this repository.

## Lifecycle

`CodexAppServerClient` starts the configured command as a local child process
with UTF-8 stdin, stdout, and stderr pipes. Its JSONL lifecycle is:

1. `initialize` request and matching response;
2. `initialized` notification;
3. `thread/start` with the configured workspace, approval policy, and sandbox;
4. `turn/start` with one text input containing the task prompt;
5. structured notifications through the matching `turn/completed`; and
6. process closure.

The app-server protocol has no turn-title field, so `WorkerTaskRequest.title`
remains Code Mule metadata and is not added to the wire request.

Only a completed `agentMessage` delivered by `item/completed` becomes the final
message. Phase 7 passes a strict native `outputSchema` on every Worker turn and
validates that final message as one structured execution report. Delta events,
stderr output, and prose keywords do not control task status. The first request
timeout terminates the child process and is not retried automatically. At most
the last 100 stderr lines are retained for local diagnostics.

## Approval and Input Boundary

Code Mule never approves an app-server request. Command, file-change,
permission, and legacy approval requests raise `CodexApprovalRequired`.
Interactive tool input and MCP elicitation requests raise
`CodexUserInputRequired`. Unknown server requests and malformed messages fail
closed as protocol errors.

The caller selects an app-server-supported sandbox and approval policy. A
read-only inspection should use:

```python
CodexWorkerConfig(
    command=("codex", "app-server"),
    workspace=absolute_workspace,
    approval_policy="on-request",
    sandbox="read-only",
    read_timeout_seconds=120,
)
```

The workspace must be an absolute path. The contract contains no API key or
token field and does not read credentials. The app-server child environment
removes API key, token, secret, password, and credential variables inherited
from the Supervisor/CLI process. In particular, the DeepSeek key is never
inherited by the Worker. Local Codex authentication continues through its
configured local app environment; provider credentials are not placed in
prompts or protocol payloads.

## Execution Reports

`CodexWorkerService.execute` runs one task through a fresh app-server process.
`CodexWorkerSession` additionally supports multiple turns on one thread for a
bounded REWORK cycle. Native structured output supplies execution status,
summary, files changed, individual checks, Git state, issues, and the Human
Gate flag. Every field is validated locally without prose parsing; `not_run`
and `unknown` remain explicit evidence states rather than implied success.

Project State Store remains the source of truth. The Worker neither mutates a
`Task` nor advances project state; the deterministic Phase 7 task-cycle service
persists reports and applies review outcomes. See [Single-Task Autonomous
Cycle](task-cycle.md).
