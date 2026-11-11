"""SATVIQ Candidate Intelligence — canonical AI evaluation JSON.

Public surface:
  SCHEMA_VERSION          'ai-evaluation/1.1'
  build_canonical(record) deterministic projection (no AI calls)
  validate_canonical(d)   strict Pydantic validation (raises)
  check_canonical(d)      (ok, model | [errors]) non-raising form
"""

from .builder import build_canonical
from .schema import SCHEMA_VERSION, CanonicalEvaluation
from .validators import check_canonical, validate_canonical

__all__ = ["SCHEMA_VERSION", "CanonicalEvaluation", "build_canonical",
           "validate_canonical", "check_canonical"]
