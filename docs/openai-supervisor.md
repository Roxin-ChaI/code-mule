# OpenAI Supervisor Provider

## Purpose

`OpenAISupervisorModelClient` is a retained provider implementation for the Code Mule Supervisor reasoning boundary. It uses the OpenAI Responses API with JSON Schema Structured Outputs and returns a plain JSON-compatible object to the existing local parser. See the official [Create a model response](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) reference.

This implementation is not Code Mule's configured real Supervisor provider. The configured project path is [DeepSeek](deepseek-supervisor.md); it uses the official `openai` Python SDK only as an OpenAI-compatible transport client pointed at the DeepSeek endpoint. Code Mule does not require an OpenAI API key or an OpenAI real E2E run.

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

## Real E2E Status

Code Mule has no configured real OpenAI E2E path. Real Supervisor validation uses the Boss-only manual DeepSeek procedure documented in [DeepSeek Supervisor Provider](deepseek-supervisor.md). The OpenAI implementation and its fake-client unit and integration tests remain available to preserve the provider abstraction.
