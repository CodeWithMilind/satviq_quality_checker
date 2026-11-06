"""Simple production resume extraction pipeline.

Tika + PyMuPDF + Docling (concurrent) -> Quality Evaluation ->
Best Extractor Selection -> Text Normalization -> One LLM Stage ->
Validated Resume JSON.

The legacy sequential fallback chain (extractor.ExtractionManager +
QualityGatePolicy in api.pipeline) is unchanged and remains the
default; this package is the additive simple pipeline.
"""
from .config import PipelineConfig, load_pipeline_config
from .extraction_pipeline import ExtractionPipeline
from .quality import evaluate_extraction_quality
from .selector import select_best_extractor

__all__ = [
    "ExtractionPipeline",
    "PipelineConfig",
    "load_pipeline_config",
    "evaluate_extraction_quality",
    "select_best_extractor",
]
