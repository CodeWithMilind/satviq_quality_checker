"""AI tasks built on providers."""

from .resume_recovery import ResumeRecoveryTask, recover_resume
from .text_recovery import TextRecoveryTask, recover_text

__all__ = ["ResumeRecoveryTask", "recover_resume", "TextRecoveryTask", "recover_text"]
