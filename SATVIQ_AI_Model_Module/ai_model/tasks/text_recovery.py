"""Text-only recovery task.

A lighter recovery path for cases without page images: one corrupted
extraction plus reference extractions from other tools. Same failure
contract as resume recovery — errors propagate, nothing is fabricated.
"""

from __future__ import annotations

from ..prompts.text_recovery_prompt import (
    TEXT_RECOVERY_SYSTEM_PROMPT,
    build_text_recovery_prompt,
)
from ..providers.base_provider import BaseProvider
from ..schemas import (
    ModelDefinition,
    TextRecoveryInput,
    TextRecoveryResult,
    _TextRecoveryPayload,
)


class TextRecoveryTask:
    def __init__(self, provider: BaseProvider, model: ModelDefinition) -> None:
        self._provider = provider
        self._model = model

    @property
    def model(self) -> ModelDefinition:
        return self._model

    def run(self, data: TextRecoveryInput | dict) -> TextRecoveryResult:
        request = (
            data if isinstance(data, TextRecoveryInput)
            else TextRecoveryInput.model_validate(data)
        )
        prompt = build_text_recovery_prompt(
            text_id=request.text_id,
            corrupted_text=request.corrupted_text,
            reference_texts=request.reference_texts,
            quality_report=request.quality_report,
        )
        response = self._provider.generate_structured_output(
            model_id=self._model.model_id,
            model_name=self._model.name,
            prompt=prompt,
            system_prompt=TEXT_RECOVERY_SYSTEM_PROMPT,
            response_schema=_TextRecoveryPayload,
        )
        payload: _TextRecoveryPayload = response.parsed  # type: ignore[assignment]
        return TextRecoveryResult(
            text_id=request.text_id,
            recovered_text=payload.recovered_text,
            recovery_confidence=payload.recovery_confidence,
            issues=payload.issues,
            model_name=self._model.name,
            provider=self._provider.name,
            success=True,
            token_usage=response.token_usage,
        )


def recover_text(
    provider: BaseProvider,
    model: ModelDefinition,
    data: TextRecoveryInput | dict,
) -> TextRecoveryResult:
    """Functional shortcut around :class:`TextRecoveryTask`."""
    return TextRecoveryTask(provider, model).run(data)
