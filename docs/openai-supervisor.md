# OpenAI Supervisor Provider

## Purpose

OpenAI is the first concrete provider for the Code Mule Supervisor reasoning boundary. The adapter uses the OpenAI Responses API with JSON Schema Structured Outputs and returns a plain JSON-compatible object to the existing local parser. See the official [Create a model response](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) reference.

## Architecture

```text
SupervisorService
  → SupervisorModelClient
  → OpenAISupervisorModelClient
  → OpenAI Responses API
  → JSON object
  → local Supervisor parser
  → typed result
```

The adapter sends the shared Supervisor policy through `instructions`, the current ProjectState-derived prompt through `input`, and the Phase 4 response schema through `text.format` with `type: json_schema` and `strict: true`. It accepts only a `completed` response with non-empty `output_text` containing a JSON object.

## State Boundary

ProjectState remains the Source of Truth. Each request is independent and sets `store=False`. The provider does not use the Conversations API, `conversation`, or `previous_response_id`, and OpenAI-hosted conversation state is not a correctness dependency.

## Authentication

Authentication belongs to runtime composition. `OpenAISupervisorModelClient` receives an already constructed compatible client and never reads environment variables or stores an API key.

When constructing a real OpenAI client, set `max_retries=0` so SDK transport retries do not weaken Code Mule's single-request contract.

## Automated Tests

All automated provider and service-integration tests use an injected fake `responses.create` surface. They make no network requests, read no API key, and produce no API charges.

## Manual Real E2E

The manual script makes a real OpenAI API request and may incur charges. It must be run only by a user who has reviewed and approved that external action.

From the repository root:

```bash
source .venv/bin/activate
export OPENAI_API_KEY="..."
export CODE_MULE_OPENAI_MODEL="..."
python3.12 scripts/manual_openai_supervisor_e2e.py
```

The script prints a prominent warning before validating configuration. It never prints or persists the API key. It constructs a minimal deterministic ProjectState and performs one `report_progress()` request without tools, repository mutation, Codex, deployment, or Git operations.
