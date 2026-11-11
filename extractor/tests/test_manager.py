"""Tests for the ExtractionManager, using mocked extractors.

No real Tika / Docling / Tesseract installation is required.
"""

from pathlib import Path

import pytest

from extractor.base import BaseExtractor
from extractor.manager import BasicAcceptancePolicy, ExtractionManager
from extractor.models import ExtractionResult

GOOD_TEXT = ("Jane Doe is a software engineer with experience in Python, "
             "FastAPI, REST APIs, SQL databases and data pipeline work.")
SHORT_TEXT = "Too short."


class MockExtractor(BaseExtractor):
    """Scripted extractor: behaviour is one of ok / fail / raise / short."""

    def __init__(self, name, behaviour, calls):
        super().__init__(config={})
        self.name = name
        self.behaviour = behaviour
        self.calls = calls

    def _extract(self, path: Path) -> ExtractionResult:
        self.calls.append(self.name)
        if self.behaviour == "raise":
            raise RuntimeError(f"{self.name} exploded")
        if self.behaviour == "fail":
            return self._failure(path, f"{self.name} could not extract this file")
        if self.behaviour == "short":
            return self._success(path, SHORT_TEXT)
        return self._success(path, GOOD_TEXT)


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-fake")
    return path


def _manager(behaviours: dict[str, str], calls: list[str], **manager_overrides):
    extractors = {name: MockExtractor(name, behaviour, calls)
                  for name, behaviour in behaviours.items()}
    config = {"manager": {"order": list(behaviours), "max_attempts": 10,
                          "max_total_timeout_seconds": 60}}
    config["manager"].update(manager_overrides)
    return ExtractionManager(config=config, extractors=extractors)


def test_successful_first_extractor_no_fallback(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "ok", "pymupdf": "ok"}, calls)
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "tika"
    assert result.text == GOOD_TEXT
    assert calls == ["tika"]  # second extractor never tried
    assert len(result.attempts) == 1
    assert result.attempts[0].accepted is True


def test_fallback_after_failure(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "fail", "pymupdf": "ok", "ocr": "ok"}, calls)
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "pymupdf"
    assert calls == ["tika", "pymupdf"]
    assert result.attempts[0].success is False
    assert "could not extract" in result.attempts[0].error


def test_fallback_after_exception(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "raise", "pymupdf": "ok"}, calls)
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "pymupdf"
    assert "exploded" in result.attempts[0].error


def test_fallback_after_short_text_rejected_by_policy(sample_file):
    """Fallback is driven by the acceptance policy, not only exceptions."""
    calls: list[str] = []
    manager = _manager({"tika": "short", "pymupdf": "ok"}, calls)
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "pymupdf"
    assert calls == ["tika", "pymupdf"]
    assert result.attempts[0].success is True       # extraction itself worked
    assert result.attempts[0].accepted is False     # but policy rejected it
    assert "acceptance policy" in result.attempts[0].error


def test_correct_extractor_order(sample_file):
    """Attempts must follow the configured order exactly (here the
    deliberately non-default order ocr -> tika -> pymupdf)."""
    calls: list[str] = []
    manager = _manager({"ocr": "fail", "tika": "fail", "pymupdf": "ok"}, calls)
    assert manager.order == ["ocr", "tika", "pymupdf"]
    result = manager.extract(sample_file)
    assert result.success is True
    assert calls == ["ocr", "tika", "pymupdf"]


def test_configured_order_is_respected(sample_file):
    calls: list[str] = []
    extractors = {"a": MockExtractor("a", "fail", calls),
                  "b": MockExtractor("b", "ok", calls)}
    config = {"manager": {"order": ["b", "a"], "max_attempts": 10}}
    manager = ExtractionManager(config=config, extractors=extractors)
    result = manager.extract(sample_file)
    assert result.extractor_name == "b"
    assert calls == ["b"]


def test_all_extractors_failing(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "fail", "pymupdf": "raise", "ocr": "fail"}, calls)
    result = manager.extract(sample_file)
    assert result.success is False
    assert result.error.startswith("All extractors failed")
    assert len(result.attempts) == 3
    # No failure reason is silently discarded:
    for attempt in result.attempts:
        assert attempt.error
    assert "tika" in result.error and "pymupdf" in result.error


def test_duplicate_attempts_prevented(sample_file):
    calls: list[str] = []
    extractors = {"tika": MockExtractor("tika", "fail", calls),
                  "pymupdf": MockExtractor("pymupdf", "ok", calls)}
    config = {"manager": {"order": ["tika", "tika", "tika", "pymupdf"],
                          "max_attempts": 10}}
    manager = ExtractionManager(config=config, extractors=extractors)
    result = manager.extract(sample_file)
    assert result.success is True
    assert calls == ["tika", "pymupdf"]  # tika attempted exactly once


def test_max_attempts_limit(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "fail", "pymupdf": "fail", "ocr": "ok"}, calls,
                       max_attempts=1)
    result = manager.extract(sample_file)
    assert result.success is False
    assert calls == ["tika"]


def test_disabled_extractor_is_skipped(sample_file):
    calls: list[str] = []
    extractors = {"tika": MockExtractor("tika", "ok", calls),
                  "pymupdf": MockExtractor("pymupdf", "ok", calls)}
    config = {"manager": {"order": ["tika", "pymupdf"], "max_attempts": 10},
              "extractors": {"tika": {"enabled": False}}}
    manager = ExtractionManager(config=config, extractors=extractors)
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "pymupdf"
    assert calls == ["pymupdf"]


def test_total_timeout_stops_attempts(sample_file):
    # The configurable extraction safety budget still functions
    # (spec §5): with a zero budget no attempt is started.
    calls: list[str] = []
    manager = _manager({"tika": "fail", "pymupdf": "ok"}, calls,
                       max_total_timeout_seconds=0)
    result = manager.extract(sample_file)
    assert result.success is False
    assert calls == []
    assert "timeout" in result.error.lower()


def test_invalid_file_path(tmp_path):
    manager = _manager({"tika": "ok"}, [])
    result = manager.extract(tmp_path / "missing.pdf")
    assert result.success is False
    assert "not found" in result.error.lower()
    assert result.attempts == []


def test_configuration_overrides(sample_file):
    policy = BasicAcceptancePolicy({"min_text_chars": 5, "min_word_count": 2})
    assert policy.accept(ExtractionResult(
        extractor_name="x", success=True, text=SHORT_TEXT)) is True
    manager = ExtractionManager(
        config={"manager": {"order": ["ocr", "tika"]},
                "extractors": {"ocr": {"enabled": False}}},
    )
    assert manager.order == ["ocr", "tika"]
    assert manager.is_enabled("ocr") is False
    assert manager.is_enabled("tika") is True


def test_default_order_comes_from_yaml_config():
    manager = ExtractionManager()
    assert manager.order == ["tika", "pymupdf", "docling", "ocr"]
    assert manager.max_attempts == 4
    assert all(manager.is_enabled(name) for name in manager.order)


def test_extraction_result_structure(sample_file):
    calls: list[str] = []
    manager = _manager({"tika": "ok"}, calls)
    result = manager.extract(sample_file)
    dumped = result.model_dump()
    for field in ["text", "extractor_name", "success", "file_type", "metadata",
                  "page_count", "page_texts", "warnings", "error", "duration_ms"]:
        assert field in dumped
    assert isinstance(result.metadata, dict)
    assert isinstance(result.page_texts, list)
    assert isinstance(result.warnings, list)
    restored = ExtractionResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_empty_document_is_distinguishable_from_failure():
    """A successful extraction of an empty document (success=True,
    empty text) is a different state from an extraction failure."""
    empty_ok = ExtractionResult(extractor_name="mock", success=True, text="")
    failed = ExtractionResult(extractor_name="mock", success=False, text="",
                              error="parse error")
    assert empty_ok.success is True and empty_ok.error is None
    assert failed.success is False and failed.error is not None
    policy = BasicAcceptancePolicy()
    assert policy.accept(empty_ok) is False  # manager would fall back, with reason recorded
    assert "not meaningful enough" in policy.describe_rejection(empty_ok)


def test_quality_checker_extension_point(sample_file):
    """A policy object with accept()/describe_rejection() can replace the
    basic policy — this is where a future Quality Checker plugs in,
    without the manager importing or recreating it."""

    class FakeQualityPolicy:
        def accept(self, result: ExtractionResult) -> bool:
            return result.success and "engineer" in result.text.lower()

        def describe_rejection(self, result: ExtractionResult) -> str:
            return "Rejected by (fake) quality policy."

    calls: list[str] = []
    extractors = {"tika": MockExtractor("tika", "short", calls),
                  "pymupdf": MockExtractor("pymupdf", "ok", calls)}
    config = {"manager": {"order": ["tika", "pymupdf"], "max_attempts": 10}}
    manager = ExtractionManager(config=config, extractors=extractors,
                                acceptance_policy=FakeQualityPolicy())
    result = manager.extract(sample_file)
    assert result.success is True
    assert result.extractor_name == "pymupdf"
    assert result.attempts[0].error == "Rejected by (fake) quality policy."
