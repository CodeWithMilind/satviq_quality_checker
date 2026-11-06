# SATVIQ — `ai_model/` Module

An **independent** AI model module for the Resume Extraction and
Quality Checker system. It manages the AI models used for resume
recovery and quality improvement.

It does **not** import or depend on the Extractor, Quality Checker, or
Frontend. Extraction results and quality reports reach it only as
plain function arguments, so it can be dropped into the existing
project unchanged and integrated later:

```text
Extractor → Quality Checker → (if quality fails) → AIService.recover_resume()
          → Recovered Text → Quality Checker again
```

That pipeline wiring is intentionally **not** implemented here — only
the AI module itself.

## Architecture

```text
ai_model/
├── __init__.py            Public exports (AIService, config, schemas, exceptions)
├── config.py              Environment-based config (AIModelConfig)
├── exceptions.py          AIModelError + ModelNotFoundError, UnsupportedCapabilityError,
│                          ModelAPIError, ModelTimeoutError, InvalidModelOutputError,
│                          ImageProcessingError, ConfigurationError, ProviderNotAvailableError
├── schemas.py             ModelDefinition, ResumeRecoveryInput/Result, TextRecoveryInput/Result
├── model_registry.py      Central registry of models + capabilities
├── model_router.py        Task-based selection; every selection is recorded
├── ai_service.py          Public facade: AIService
├── providers/
│   ├── base_provider.py   Abstract interface: generate_text / generate_structured_output /
│   │                      analyze_images / health_check
│   ├── openai_provider.py OpenAI SDK implementation (own retry policy, JSON-mode
│   │                      structured output validated locally with Pydantic)
│   └── local_provider.py  Placeholder for a future Ollama/local backend
├── models/
│   └── model_definitions.py   GPT-4o-mini registered by default; GPT-4o and a local
│                              placeholder defined but disabled, as extension examples
├── tasks/
│   ├── resume_recovery.py Vision recovery from page images + extractor outputs
│   └── text_recovery.py   Text-only recovery from corrupted + reference texts
├── prompts/
│   ├── resume_recovery_prompt.py  System prompt: images are the source of truth,
│   │                              never invent, mark [UNREADABLE], preserve order
│   └── text_recovery_prompt.py
├── utils/
│   ├── image_utils.py     PNG/JPEG validation (magic bytes), downscaling, base64 /
│   │                      data-URI / path inputs, per-page processing. No PDF work here.
│   └── token_utils.py     Dependency-free token estimation + context fitting/truncation
└── tests/                 67 unit tests, fully mocked — no real API key or network
```

### Key design decisions

- **No hardcoded model selection.** Tasks ask the router; the router
  asks the registry. Adding a model = adding one `ModelDefinition`
  (in `models/model_definitions.py` or at runtime via
  `registry.register(...)`). Adding a provider = implementing
  `BaseProvider` in one new file.
- **Routing is auditable.** `ModelRouter.selection_history` records
  task, model, provider, capabilities, and time for every selection,
  and every result carries `model_name` + `provider` — the module
  never silently switches models.
- **Structured output only.** The model returns JSON validated
  against a Pydantic payload schema (`recovery_confidence` must be
  0–1, recovered text must be non-empty). The service fills in
  `resume_id`, `model_name`, `provider`, and `success` from values it
  knows — the model is never trusted to echo them.
- **Failure is explicit, never fabricated.** Timeouts, rate limits,
  API errors, and malformed outputs raise module exceptions
  (`ModelTimeoutError`, `ModelAPIError`, `InvalidModelOutputError`,
  …). Only transient errors are retried (up to
  `AI_MODEL_MAX_RETRIES`); auth and validation errors fail at once.
  When a call fails there is **no** result object at all.
- **Recovery honesty.** The system prompt makes the page images the
  source of truth, forbids inventing names/dates/skills/education/
  experience, requires logical reading order, and requires unreadable
  content to appear as `[UNREADABLE]` in the text and be listed in
  `unreadable_sections`.

## Configuration

Environment variables only — keys are never hardcoded, logged, or
shown in `repr`. See `.env.example`:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
AI_PROVIDER=openai
AI_MODEL_TIMEOUT=60
AI_MODEL_MAX_RETRIES=2
AI_MODEL_ENABLED=true
```

Invalid values (negative timeout, non-boolean `AI_MODEL_ENABLED`, …)
fail validation. A missing `OPENAI_API_KEY` raises
`ConfigurationError` only when the OpenAI provider is actually used,
so the rest of the system can run without a key.

## Usage

```python
from ai_model.ai_service import AIService

service = AIService()  # config from environment

result = service.recover_resume(
    resume_id="resume_001",
    page_images=page_images,            # bytes / base64 / data URIs / paths, PNG or JPEG
    extraction_results={
        "tika": "...",
        "fallback_1": "...",
        "fallback_2": "...",
    },
    quality_report={...},               # the Quality Checker's report dict
)

print(result.recovered_text)
print(result.recovery_confidence)       # 0.0 – 1.0
print(result.unreadable_sections)
print(result.model_name, result.provider)   # gpt-4o-mini, openai
```

Model selection, as in the spec:

```python
model = service.select_model(
    task="resume_recovery",
    required_capabilities=["vision", "structured_output"],
)
```

Text-only recovery (no images) is available as
`service.recover_text(text_id=..., corrupted_text=..., reference_texts=...)`.

See `example_integration.py` for a complete caller-side example,
including the quality-gate check that decides whether to call the AI
at all.

## Running the tests

```bash
pip install -r requirements.txt
python -m pytest ai_model/tests -q
```

Expected: **67 passed**. All OpenAI responses are mocked through an
injected fake client — the suite needs no API key and makes no
network calls. It covers the registry, capability validation, routing
(including disabled/unknown models), the provider abstraction,
structured-output parsing and rejection of malformed output, retry
behaviour for timeouts/rate limits/5xx vs. no-retry for auth errors,
missing API key, image validation and downscaling, and the rule that
an API failure produces an exception — never a fabricated result.

## Extending later

- **New OpenAI model:** add a `ModelDefinition` in
  `models/model_definitions.py` and include it in
  `default_model_definitions()`.
- **Local SLM (Ollama):** implement the four `BaseProvider` methods
  in `providers/local_provider.py`, enable the local model
  definition, set `AI_PROVIDER=local`. Nothing else changes.
- **Vision-only or other providers:** same pattern — one provider
  class, one or more model definitions.
