"""Resume recovery task.

Given page images (the source of truth), the text produced by several
extractors, and the Quality Checker's report, ask the selected model
to reconstruct the original resume text as structured output.

Failure contract: provider errors and malformed model output
propagate as module exceptions. This task NEVER fabricates a recovery
result when the model call fails.
"""

from __future__ import annotations

from ..config import AIModelConfig
from ..prompts.resume_recovery_prompt import (
    RESUME_RECOVERY_SYSTEM_PROMPT,
    build_resume_recovery_prompt,
)
from ..providers.base_provider import BaseProvider
from ..schemas import (
    ModelDefinition,
    ResumeRecoveryInput,
    ResumeRecoveryResult,
    _RecoveryPayload,
)
from ..utils.image_utils import prepare_images


class ResumeRecoveryTask:
    """Executes resume recovery against one provider + selected model."""

    def __init__(
        self,
        provider: BaseProvider,
        model: ModelDefinition,
        config: AIModelConfig | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._config = config or AIModelConfig()

    @property
    def model(self) -> ModelDefinition:
        return self._model

    def run(self, data: ResumeRecoveryInput | dict) -> ResumeRecoveryResult:
        request = (
            data if isinstance(data, ResumeRecoveryInput)
            else ResumeRecoveryInput.model_validate(data)
        )

        prepared_images = prepare_images(
            request.page_images,
            max_dimension=self._config.max_image_dimension,
            max_bytes=self._config.max_image_bytes,
        )

        prompt = build_resume_recovery_prompt(
            resume_id=request.resume_id,
            extraction_results=request.extraction_results,
            quality_report=request.quality_report,
            page_count=len(prepared_images),
        )

        response = self._provider.generate_structured_output(
            model_id=self._model.model_id,
            model_name=self._model.name,
            prompt=prompt,
            system_prompt=RESUME_RECOVERY_SYSTEM_PROMPT,
            response_schema=_RecoveryPayload,
            images=prepared_images,
        )
        payload: _RecoveryPayload = response.parsed  # type: ignore[assignment]

        return ResumeRecoveryResult(
            resume_id=request.resume_id,
            recovered_text=payload.recovered_text,
            recovery_confidence=payload.recovery_confidence,
            unreadable_sections=payload.unreadable_sections,
            issues=payload.issues,
            model_name=self._model.name,
            provider=self._provider.name,
            success=True,
            token_usage=response.token_usage,
        )


def recover_resume(
    provider: BaseProvider,
    model: ModelDefinition,
    data: ResumeRecoveryInput | dict,
    config: AIModelConfig | None = None,
) -> ResumeRecoveryResult:
    """Functional shortcut around :class:`ResumeRecoveryTask`."""
    return ResumeRecoveryTask(provider, model, config).run(data)
