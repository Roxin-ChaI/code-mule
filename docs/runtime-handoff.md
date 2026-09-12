# Runtime Handoff and Local Launch

Project completion and product launch are separate boundaries. Code Mule records a
verified, revision-scoped `DeliveryManifest` before a newly initialized project can
become `DONE`. The manifest says what was delivered, where its entry point is, how
it can be used, and—only when applicable—how a local process can be started,
checked, and stopped.

## Boss workflow

```bash
code-mule status
code-mule deliverable
code-mule launch
code-mule app-status
code-mule stop-app
```

`deliverable` is read-only. `launch` is allowed only for the current completed
revision when its manifest, completion Git HEAD, clean workspace, required
environment, local port, and launch command all validate. A library, component,
package, or documentation result is still a valid delivery; `launch` explains its
usage and starts no process.

## Manifest candidate

The final Task writes `code-mule-delivery.json` at the target repository root.
Code Mule treats this file as untrusted candidate data and materializes it into
ProjectState only after strict deterministic validation. A runnable static site can
use this shape:

```json
{
  "deliverable_type": "static_web",
  "runnable": true,
  "entry_point": "index.html",
  "launch_spec": {
    "command": {
      "executable": "python3",
      "args": ["-m", "http.server", "{port}", "--bind", "127.0.0.1"]
    },
    "working_directory": ".",
    "environment_keys": [],
    "startup_timeout_seconds": 10,
    "expected_long_running": true,
    "requires_args": false,
    "supports_dynamic_port": true
  },
  "verification_spec": {
    "required_paths": ["index.html"],
    "launch_smoke_test_supported": true
  },
  "health_check_spec": {
    "type": "http",
    "url": "http://127.0.0.1:{port}/",
    "command": null,
    "expected_status": 200,
    "timeout_seconds": 3
  },
  "access_spec": {"host": "127.0.0.1", "port": null, "path": "/"},
  "stop_spec": {"grace_seconds": 5},
  "required_environment": [],
  "runtime_generated_paths": [".code-mule/runtime"],
  "usage": "Run code-mule launch, then open the reported local URL."
}
```

Commands are an executable plus an argument array. Shell strings, destructive Git
operations, remote health checks, secret-like values, absolute/out-of-repository
paths, missing entry points, and unknown extra fields fail closed. Runtime-generated
paths must already be Git ignored; Code Mule does not rewrite `.gitignore` during
launch.

For a non-runnable library, `runnable` is `false`, health type is `none`, and
`launch_spec`, `access_spec`, and `stop_spec` are `null`. `usage` explains the import
or integration entry point.

## Process ownership and safety

A `RuntimeSession` stores the PID together with fingerprints of the OS process start
identity and command. `stop-app` sends a graceful termination signal only when all
identities still match. A reused PID or changed command becomes
`OWNERSHIP_UNCERTAIN`; Code Mule will not kill it. There is no force kill, browser
automation, container orchestration, deployment, or remote side effect in this
phase.

Full runtime log capture and an `app-logs` command are deliberately deferred.
The launcher discards child stdout/stderr instead of retaining unbounded or
potentially sensitive output.

Health checks are restricted to the owned process, structured local commands, or
plain HTTP on `localhost`, `127.0.0.1`, or `::1`. A configured fixed port conflict
fails without touching the existing listener. A manifest that permits dynamic ports
uses a free local port for that RuntimeSession without mutating the immutable
manifest.

## Revision and migration rules

Each successfully completed revision gets its own immutable manifest snapshot.
Changing and completing a later revision requires a newly validated manifest for
that revision. Schema v14 states migrate to v15 with no fabricated manifest and no
RuntimeSession; their historical project facts remain readable, but launch remains
unavailable until a future completed revision establishes verified delivery truth.
