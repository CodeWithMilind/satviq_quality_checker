"""ai_model — independent AI model module for SATVIQ Resume Intelligence.

Manages AI models used for resume recovery and quality improvement.
Independent of the extractor, quality checker, and frontend: it
receives extraction results and quality reports through function
arguments only.

Public entry point::

    from ai_model import AIService
"""

from .ai_service import AIService
from .config import AIModelConfig
from .exceptions import (
    AIModelError,
    ConfigurationError,
    ImageProcessingError,
    InvalidModelOutputError,
    ModelAPIError,
    ModelNotFoundError,
    ModelTimeoutError,
    ProviderNotAvailableError,
    UnsupportedCapabilityError,
)
from .model_registry import ModelRegistry, create_default_registry
from .model_router import ModelRouter
from .schemas import (
    ModelDefinition,
    ResumeRecoveryInput,
    ResumeRecoveryResult,
    TextRecoveryInput,
    TextRecoveryResult,
)

__version__ = "1.0.0"

__all__ = [
    "AIService",
    "AIModelConfig",
    "AIModelError",
    "ConfigurationError",
    "ImageProcessingError",
    "InvalidModelOutputError",
    "ModelAPIError",
    "ModelNotFoundError",
    "ModelTimeoutError",
    "ProviderNotAvailableError",
    "UnsupportedCapabilityError",
    "ModelRegistry",
    "ModelRouter",
    "create_default_registry",
    "ModelDefinition",
    "ResumeRecoveryInput",
    "ResumeRecoveryResult",
    "TextRecoveryInput",
    "TextRecoveryResult",
    "__version__",
]
