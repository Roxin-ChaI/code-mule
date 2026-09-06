# Boss Chat

`code-mule chat` provides a persistent natural-language Boss interface over
the existing deterministic CLI capabilities. It is a presentation and routing
layer, not an alternative state machine.

```bash
code-mule chat
code-mule chat --verbose
```

Obvious read-only queries and controls are routed deterministically. Ambiguous
language may use the structured Supervisor router when a DeepSeek key is
available. The model returns only a typed intent; it never mutates ProjectState
or directly invokes a Worker. Low-confidence or ambiguous side-effect requests
ask for clarification.

Examples include:

- “现在做到哪了？” — persisted Task progress
- “完整计划是什么？” — active Plan and dependencies
- “有什么需要我处理的吗？” — blockers and pending Human Actions
- “项目为什么停了？” / “what should I do next?” — the same deterministic
  ProjectDiagnosis used by `code-mule diagnose`
- “暂停” / “继续” — existing PAUSE / RESUME commands
- “增加导出 CSV” — existing CHANGE submission, followed by explicit apply
- “这个项目不做了” — typed STOP and cancellation Safe Point

Facts always come from ProjectState. Language generation is optional
presentation only. `--verbose` exposes the routed intent and referenced IDs;
default output keeps raw enums and internal IDs hidden.

Explicit diagnosis phrases are routed without a model call, including “项目诊断”,
“现在卡在哪里”, “为什么不能继续”, `diagnose project`, `why is it blocked`,
and `how can I continue`. Diagnosis remains read-only; it does not resolve the
reported blocker or start execution.

Chat does not start a second Worker when another CLI process owns execution.
It observes persisted state and can submit supported control commands. Execution
ownership, stale recovery, action-scoped approvals, and all Human Gates remain
enforced by their existing deterministic services.
