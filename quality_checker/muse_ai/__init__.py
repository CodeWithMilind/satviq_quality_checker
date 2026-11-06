"""Muse AI integration — the project's only LLM provider.

Configured exclusively through backend environment variables
(see :mod:`muse_ai.config` and `.env.example`). Used by the
extraction pipeline for Resume JSON generation and for direct
image/PDF extraction when the rule-based chain cannot produce a
passing result. The Quality Checker never calls this package.
"""

from .client import (MuseClient, MuseError, MuseAuthError,
                     MuseConfigurationError, MuseTimeoutError)
from .config import (PROVIDER_NAME, MuseConfig, load_env_file,
                     load_muse_config)
from .schema import ResumeJSON, Skills, validate_resume_json
from .service import MuseService

__all__ = [
    "MuseClient", "MuseError", "MuseAuthError",
    "MuseConfigurationError", "MuseTimeoutError",
    "PROVIDER_NAME", "MuseConfig", "load_env_file",
    "load_muse_config", "ResumeJSON", "validate_resume_json",
    "MuseService", "Skills",
]
