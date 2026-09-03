# Supervisor reliability and bounded regeneration

Code Mule classifies every failed Supervisor attempt before deciding whether a
fresh response may be requested. Classification is typed; runtime control never
depends on matching exception text.

## Failure categories

Retryable model-output and temporary transport failures are:

- `TRANSPORT_TIMEOUT`
- `TEMPORARY_CONNECTION_FAILURE`
- `INCOMPLETE_MAX_OUTPUT_TOKENS`
- `MALFORMED_STRUCTURED_RESPONSE`
- `SCHEMA_CONTRACT_VIOLATION`
- `DECISION_CONTRACT_VIOLATION`

Non-retryable safety and provider failures are:

- `CONTENT_FILTER`
- `DETERMINISTIC_VALIDATION_FAILURE`
- `INVALID_BUSINESS_REFERENCE`
- `HUMAN_GATE`
- `PROVIDER_AUTHENTICATION`
- `PROVIDER_CONFIGURATION`
- `UNKNOWN_FAILURE`

DeepSeek incomplete responses retain their typed incomplete reason.
`max_output_tokens` is retryable; `content_filter` is not. Typed SDK timeout and
connection failures may be retried. Authentication and configuration failures
stop after one attempt. The SDK remains configured with `max_retries=0`, so all
retry accounting belongs to Code Mule.

## Bounded attempts

`SupervisorRetryPolicy` defaults to `max_attempts=2`: one initial request and at
most one regeneration request. Attempts are numbered from one. A fixed delay may
be configured and the sleeper is injectable for deterministic tests.

The policy applies uniformly to PLAN, REVIEW, IMPACT_ANALYSIS, and
PROGRESS_REPORT. There is no infinite retry and no fallback model.

## Regeneration is not repair

On a retryable failure, Code Mule sends a new provider request for a complete
response using the same operation schema. The second request receives only a
short contract reminder. It never receives the old response.

Code Mule does not:

- remove extra fields;
- insert missing fields or null values;
- extract or patch JSON fragments;
- fuzzy-match identifiers;
- mutate a previous decision;
- bypass the local parser or validator.

The original request context is rebuilt with a minimal instruction to return a
fresh, schema-complete response.

## Validation boundary

Structured-output parsing occurs inside the bounded Supervisor call. Malformed
JSON objects, schema field drift, and illegal REVIEW field combinations may be
regenerated because they are model-output failures.

Deterministic domain validation remains outside this loop. Unknown Requirement
or Task IDs, ID collisions, invalid dependency edges, dependency cycles, and
other business-reference violations are rejected immediately. They do not cause
another model call and are never converted into a structurally acceptable plan.

A valid REVIEW decision of `HUMAN_REQUIRED` is also not a failure to regenerate;
it remains a Human Gate handled by the existing TaskCycle semantics.

## Exhaustion and human fallback

After all retryable attempts fail, `SupervisorCallFailure` carries only:

- operation;
- final typed failure category;
- attempt count;
- whether the category was retryable;
- whether the attempt budget was exhausted.

The existing planning, replanning, or TaskCycle failure boundary then creates a
typed `HumanAction(category=SUPERVISOR_FAILURE)` and moves the project to
HUMAN_REQUIRED. Project audit metadata preserves operation, failure category,
and attempt count. Raw prompts and model responses are not persisted.

## Progress

Ephemeral progress uses:

- `supervisor.retrying`
- `supervisor.retry_succeeded`
- `supervisor.retry_exhausted`

A retry is shown as `Supervisor response invalid. Regenerating (2/2)...`.
Telemetry includes only operation, attempt, and failure category.

## Local reliability E2E

The local harness uses a deterministic fake model provider and fake Worker; it
does not read credentials or make network requests:

```bash
.venv/bin/python scripts/local_supervisor_reliability_e2e.py
```

It verifies:

1. an invalid REVIEW regenerates once, then one Worker session completes;
2. two invalid PLAN responses exhaust the budget and create HUMAN_REQUIRED;
3. a structurally valid proposal with an unknown Requirement ID receives zero
   retries and is rejected by deterministic validation.
