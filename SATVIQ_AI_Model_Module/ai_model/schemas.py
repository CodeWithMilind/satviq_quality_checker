"""Pydantic schemas shared across the ``ai_model`` module.

These schemas are the module's public data contract. They deliberately
do not import anything from the extractor, quality checker, or
frontend: extraction results and quality reports arrive as plain
dicts through function arguments.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputType(str, Enum):
    TEXT = "text"
    IMAGE = "image"


# Capability names understood by the registry and router.
CAPABILITY_VISION = "vision"
CAPABILITY_STRUCTURED_OUTPUT = "structured_output"
CAPABILITY_TEXT = "text"
CAPABILITY_IMAGE = "image"
KNOWN_CAPABILITIES = {
    CAPABILITY_VISION,
    CAPABILITY_STRUCTURED_OUTPUT,
    CAPABILITY_TEXT,
    CAPABILITY_IMAGE,
}


class ModelDefinition(BaseModel):
    """One registered AI model.

    ``name`` is the stable registry key used throughout the codebase
    (e.g. ``"gpt-4o-mini"``); ``model_id`` is the identifier the
    provider's API expects. Keeping them separate lets the registry
    rename or re-point a model without touching call sites.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    provider: str
    model_id: str
    input_types: list[InputType] = Field(default_factory=lambda: [InputType.TEXT])
    supports_vision: bool = False
    supports_structured_output: bool = False
    max_context_window: int = Field(gt=0)
    enabled: bool = True
    description: str = ""

    @property
    def capabilities(self) -> set[str]:
        caps: set[str] = set()
        if InputType.TEXT in self.input_types:
            caps.add(CAPABILITY_TEXT)
        if InputType.IMAGE in self.input_types:
            caps.add(CAPABILITY_IMAGE)
        if self.supports_vision:
            caps.add(CAPABILITY_VISION)
        if self.supports_structured_output:
            caps.add(CAPABILITY_STRUCTURED_OUTPUT)
        return caps

    def supports(self, required_capabilities: list[str] | set[str]) -> bool:
        return set(required_capabilities) <= self.capabilities


class ResumeRecoveryInput(BaseModel):
    """Input for the resume recovery task.

    ``page_images`` items may be raw bytes, base64 strings, data URIs,
    or filesystem paths (``str`` / ``os.PathLike``) — they are
    normalised by ``utils.image_utils``. Page images are produced by
    the existing Extractor; this module never renders PDFs itself.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    resume_id: str = Field(min_length=1)
    page_images: list[Any] = Field(default_factory=list)
    extraction_results: dict[str, str] = Field(
        default_factory=dict,
        description="Extracted text keyed by extractor name, e.g. {'tika': '...', 'fallback_1': '...'}",
    )
    quality_report: dict[str, Any] = Field(default_factory=dict)


class ResumeRecoveryResult(BaseModel):
    """Structured result returned by the model for resume recovery."""

    model_config = ConfigDict(extra="forbid")

    resume_id: str
    recovered_text: str
    recovery_confidence: float = Field(ge=0.0, le=1.0)
    unreadable_sections: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    model_name: str
    provider: str
    success: bool
    token_usage: dict[str, int] | None = None


class TextRecoveryInput(BaseModel):
    """Input for the simpler text-only recovery task."""

    model_config = ConfigDict(extra="forbid")

    text_id: str = Field(min_length=1)
    corrupted_text: str = ""
    reference_texts: dict[str, str] = Field(default_factory=dict)
    quality_report: dict[str, Any] = Field(default_factory=dict)


class TextRecoveryResult(BaseModel):
    """Structured result returned by the model for text recovery."""

    model_config = ConfigDict(extra="forbid")

    text_id: str
    recovered_text: str
    recovery_confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)
    model_name: str
    provider: str
    success: bool
    token_usage: dict[str, int] | None = None


class _RecoveryPayload(BaseModel):
    """The exact JSON shape the *model* must return.

    Kept separate from the public result schemas: the task layer adds
    ``resume_id`` / ``model_name`` / ``provider`` / ``success`` itself,
    from values it knows for certain, instead of trusting the model to
    echo them correctly.
    """

    model_config = ConfigDict(extra="ignore")

    recovered_text: str
    recovery_confidence: float = Field(ge=0.0, le=1.0)
    unreadable_sections: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @field_validator("recovered_text")
    @classmethod
    def _text_must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("recovered_text must not be empty")
        return value


class _TextRecoveryPayload(BaseModel):
    """The exact JSON shape the model must return for text recovery."""

    model_config = ConfigDict(extra="ignore")

    recovered_text: str
    recovery_confidence: float = Field(ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)

    @field_validator("recovered_text")
    @classmethod
    def _text_must_not_be_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("recovered_text must not be empty")
        return value
