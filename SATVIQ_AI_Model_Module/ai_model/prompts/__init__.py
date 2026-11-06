"""Prompt builders for AI tasks."""

from .resume_recovery_prompt import (
    RESUME_RECOVERY_SYSTEM_PROMPT,
    build_resume_recovery_prompt,
)
from .text_recovery_prompt import (
    TEXT_RECOVERY_SYSTEM_PROMPT,
    build_text_recovery_prompt,
)

__all__ = [
    "RESUME_RECOVERY_SYSTEM_PROMPT",
    "build_resume_recovery_prompt",
    "TEXT_RECOVERY_SYSTEM_PROMPT",
    "build_text_recovery_prompt",
]
