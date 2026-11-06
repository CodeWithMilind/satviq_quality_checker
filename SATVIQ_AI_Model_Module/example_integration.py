"""Example integration for the ai_model module.

Shows the intended future flow — Extractor → Quality Checker → (only
if quality fails) AI recovery → Quality Checker again — without
implementing that pipeline inside ai_model. The extractor and quality
checker are represented here by plain function arguments, which is
exactly how ai_model receives their output.

Run (requires OPENAI_API_KEY and real page-image files):

    python example_integration.py page1.png page2.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from ai_model import AIService
from ai_model.exceptions import AIModelError


def recover_if_quality_failed(
    service: AIService,
    *,
    resume_id: str,
    page_image_paths: list[str],
    extraction_results: dict[str, str],
    quality_report: dict,
) -> str | None:
    """Return recovered text when the quality gate failed, else None.

    In the real system, `extraction_results` comes from the Extractor
    and `quality_report` from the Quality Checker; the caller would
    pass the returned text back into the Quality Checker for a
    second pass.
    """
    if quality_report.get("status") not in {"FAIL", "WARNING"}:
        return None  # extraction was good enough; no AI call needed

    page_images = [Path(p).read_bytes() for p in page_image_paths]
    result = service.recover_resume(
        resume_id=resume_id,
        page_images=page_images,
        extraction_results=extraction_results,
        quality_report=quality_report,
    )
    print(f"Recovered with {result.model_name} via {result.provider} "
          f"(confidence {result.recovery_confidence:.2f})")
    if result.unreadable_sections:
        print(f"Unreadable sections: {result.unreadable_sections}")
    for issue in result.issues:
        print(f"Issue found: {issue}")
    return result.recovered_text


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    service = AIService()  # reads OPENAI_API_KEY etc. from environment
    print("Registered models:", [m.name for m in service.list_models()])
    try:
        text = recover_if_quality_failed(
            service,
            resume_id="resume_001",
            page_image_paths=sys.argv[1:],
            extraction_results={
                "tika": "Jane Doe\nPython Developer\nSkills: Pyth...",
                "fallback_1": "Jane Doe\nPython Dev...",
            },
            quality_report={"status": "FAIL", "text_health": {"score": 42}},
        )
    except AIModelError as exc:
        # Failures are explicit: no fabricated recovery text exists.
        print(f"AI recovery failed: {type(exc).__name__}: {exc}")
        return 2
    print("\n--- Recovered text ---\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
