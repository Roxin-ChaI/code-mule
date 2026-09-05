# Worker verification evidence

Worker completion is not verification approval or Task completion. Delivery first
checks the clean recorded baseline, unchanged HEAD, exact reported/actual paths,
and an unstaged index. It then validates Worker verification evidence before
Supervisor REVIEW and the precise local Task commit.

## Required and optional checks

Every structured Worker `tests` / `static_checks` entry has exactly `name`,
`status`, `detail`, and boolean `required`. Status is `pass`, `fail`, `not_run`,
or `unknown`. Missing fields and non-boolean `required` are rejected.

| Required | PASS | NOT_RUN | FAIL / UNKNOWN |
| --- | --- | --- | --- |
| true | Continue | Block | Block |
| false | Continue | Continue | Block |

Optional does not mean that a failing check can be ignored. No check name or
language is used to infer whether a check is optional. Git ownership rules are
unchanged. No verification failure automatically retries a Worker or commits work.

## Evidence and compatibility

ProjectState schema **v11** adds `ExecutionReport.verification_checks`, containing
typed name, check type, status and required flag, with no arbitrary detail text.
Existing `tests` / `static_checks` text remains available for presentation and
Supervisor context; it must agree with the typed fields before delivery. New
Worker reports retain only a sanitized label and status in these text fields;
arbitrary `detail` is not copied into durable evidence. Historical detail is not
rewritten by migrations.

The complete v1→v11 migration chain remains supported. Migrated v10 reports have
`verification_checks=null`, explicitly identifying legacy evidence. The legacy
adapter recognizes the historical `name: status (detail)` grammar and treats
every check as required. Unrecognized text is UNKNOWN, never repaired into PASS.
Reading a historical state does not rewrite the source file or alter its events.

## Trust boundary

Task acceptance criteria currently contain natural language, not deterministic
check IDs or a required-check specification. The Worker therefore declares
`required`. Its prompt requires acceptance-related verification to be mandatory
and forbids relabeling it as optional merely to pass delivery. Only extra checks
not demanded by acceptance criteria may be optional when unavailable.

This remains a reporting trust boundary: the deterministic gate enforces declared
semantics but cannot prove that the Worker listed every necessary check or ran it.
Supervisor REVIEW remains required after the gate; optional NOT_RUN does not
itself approve delivery. This hotfix does not invent a new check execution system.

## Failure diagnostics and human action

`WorkerVerificationError` produces `stage=verification` and a pending
`WORKER_VERIFICATION` HumanAction. Required FAIL/NOT_RUN/UNKNOWN and optional
FAIL/UNKNOWN stop before Supervisor REVIEW and before staging/committing. The
completed Worker report and partial workspace remain intact; Task is not COMPLETED.

`code-mule inspect --verbose` shows the first blocking check, its type, status and
required flag, and the verification stage. Event metadata contains only these
bounded fields, never detail or arbitrary exception messages. Check labels have
a conservative character/length policy; suspicious labels are withheld entirely.
Malformed/inconsistent evidence has a generic verification diagnostic rather
than an invented check identity. Git path/HEAD/index errors retain the existing
ownership/recovery gate; this category does not weaken Git safety.

No approval, automatic retry, or verification-only recovery is added. Boss can
inspect, acknowledge (remaining safely stopped), or explicitly stop the project
using existing controls. A later, separately authorized recovery workflow must
preserve the partial workspace and revalidate evidence before delivery. Merely
acknowledging this action never resumes the Worker or creates a commit.
