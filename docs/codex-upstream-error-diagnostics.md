# P0.4 Codex upstream error diagnostics

The RC2 probe failed after four retryable notifications and one final error,
without a local timeout or workspace edit. Its safe artifact omitted the
structured upstream error type. That omission prevents identifying the
provider failure; MCP startup errors do not establish a causal relationship.

## Protocol audit

The installed CLI (`codex-cli 0.153.4`)'s `codex app-server generate-json-schema` command was used
without starting a Worker. `v2/ErrorNotification.json` has exactly these
notification fields: `error`, `threadId`, `turnId`, and `willRetry`.
`TurnError` exposes `message`, `additionalDetails`, `codexErrorInfo`, and
optional misalignment details. Only `codexErrorInfo` is projected. The existing
allowlisted legacy `error.code` path remains compatible with older fixtures.

The generated schema defines camel-case string variants and tagged object
variants. Four object variants expose an optional `httpStatusCode`:
`httpConnectionFailed`, `responseStreamConnectionFailed`,
`responseStreamDisconnected`, and `responseTooManyFailedAttempts`.
The last variant explicitly means the response retry limit was reached.
The projection retains only HTTP status integers from 100 through 599.
There is no attempt number, retry ordinal, provider source, or independent
category field in this notification schema. `source` in the safe projection
identifies the extraction field (`codexErrorInfo` or legacy `code`); category
is a finite mapping from an allowlisted structured type, not a prose inference.

[Official App Server error documentation](https://learn.chatgpt.com/docs/app-server#errors)
describes structured error variants and upstream HTTP statuses. The generated
local schema is the authority for exact wire casing and object shapes.

The matching local rollout
`01a0fcfb-a685-7390-9ffd-5ff62f986bf4` was identified by its session workspace.
Its record types were audited without printing messages or reasoning. It does
not contain persisted error notifications, so it cannot recover the missing
code or HTTP status from this historical probe.

## Safety and persistence

Retryable and final notifications have independent fixed-size summaries:
count (at most 1,000,000,000), first and last timestamp, retryability, safe
type/category, optional HTTP status, and current thread/turn identity (at most
128 characters each). Only first and last observations are retained, even for
large streams. Metadata round-trip parsing is bounded to 4096 characters and
revalidates the typed fields before verbose presentation.

Unknown or text-only errors use `upstream_detail_unavailable`. Raw messages,
response bodies, prompts, reasoning, additional details, and credentials are
never part of these projections. No text fingerprint is generated.

Failure metadata persists the snapshot for upstream, timeout, and transport
errors. Verbose inspect displays the boundary separately from prior retryable
upstream errors. The manual probe includes the snapshot and prints retry count,
final category/code, inactivity age, local timeout flag, and independent MCP
startup-error count. The original diagnostic artifact remains unchanged.

## Execution semantics

A final non-retryable error stays `CodexTurnFailed(error_notification)` even
when no terminal event follows. No terminal event is fabricated. Waiting for
a terminal after this failure could incorrectly reclassify it as a local
timeout; this diagnostic change does not introduce that wait. Retry notices
do not refresh productive activity. Valid text deltas still refresh inactivity.
The existing inactivity/hard deadlines, automatic-retry behavior, Worker
command, and inherited Codex configuration are unchanged.

No real Codex probe, DeepSeek request, push, tag, release, or global configuration
edit is performed by this verification. A future authorized real probe is
required to identify the next actual upstream failure or demonstrate recovery.
