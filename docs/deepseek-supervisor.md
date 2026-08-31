# DeepSeek Supervisor Provider

## Purpose

DeepSeek is the configured real Supervisor provider for Code Mule. The adapter uses DeepSeek's OpenAI-compatible Responses API with JSON Schema structured output and returns a plain JSON-compatible object to the existing local parser. The official DeepSeek [Responses API guide](https://api-docs.deepseek.com/guides/responses_api/) documents the compatibility surface and the `https://api.deepseek.com` base URL.

The official [`openai` Python SDK](https://developers.openai.com/api/docs/libraries) is used only as a compatibility transport client. Code Mule does not use an OpenAI API key, OpenAI endpoint, or OpenAI billing for its configured real Supervisor path.

## Architecture

```text
SupervisorService
  → SupervisorModelClient
  → DeepSeekSupervisorModelClient
  → OpenAI SDK compatibility client
  → DeepSeek Responses API
  → JSON object
  → local Supervisor parser
  → typed result
```

The adapter sends the shared Supervisor policy through `instructions`, the current ProjectState-derived prompt through `input`, and the Phase 4 response schema through `text.format`. Its format contains only the DeepSeek-documented `type: json_schema`, `name`, and `schema` fields. It accepts only a `completed` response with non-empty `output_text` containing a JSON object. The local Phase 4 parser then performs final strict, fail-closed validation.

## State Boundary

ProjectState remains the Source of Truth. The DeepSeek Responses API is stateless, so every request contains the complete current context needed for reasoning. The adapter does not send `conversation`, `previous_response_id`, or `store`, and remote conversation state is not a correctness or recovery dependency.

## Authentication and Transport

Authentication belongs to runtime composition. `DeepSeekSupervisorModelClient` receives an already constructed compatible client and never reads environment variables or stores an API key.

The manual composition root constructs the SDK client as follows:

```python
OpenAI(
    api_key=deepseek_api_key,
    base_url="https://api.deepseek.com",
    max_retries=0,
)
```

Disabling SDK retries preserves Code Mule's single-request adapter contract. The `OpenAI` name above is the SDK client class; the service provider and billing endpoint are DeepSeek.

## Automated Tests

All automated provider and service-integration tests use an injected fake `responses.create` surface. They make no network requests, read no API key, and produce no API charges.

## Manual Real E2E

The manual script makes a real DeepSeek API request and may incur charges. Only the Boss may run it after reviewing and approving that external action. Codex and automated verification must not execute it.

From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

export DEEPSEEK_API_KEY="..."
export CODE_MULE_DEEPSEEK_MODEL="deepseek-v4-flash"
.venv/bin/python scripts/manual_deepseek_supervisor_e2e.py
```

The explicit `.venv/bin/python` command guarantees that the script runs in the environment where Code Mule and its declared runtime dependencies were installed; global Python packages are not assumed. The script prints a prominent warning before validating configuration. It never prints or persists the API key. It constructs a minimal deterministic ProjectState and performs one `report_progress()` request without tools, repository mutation, Codex, deployment, or Git operations.
