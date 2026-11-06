"""Command-line demo for the extraction engine (original project script).

Usage:
    python run_extractor.py                  # extracts sample_resume.pdf
    python run_extractor.py path/to/file.pdf # extracts that file

Uses the existing ExtractionManager with its default configuration and
acceptance policy. For the full pipeline (extraction + Quality Checker)
use the GUI / API instead:  uvicorn api.main:app  or  run.bat
"""

import sys

from extractor import ExtractionManager

manager = ExtractionManager()

result = manager.extract(sys.argv[1] if len(sys.argv) > 1 else "sample_resume.pdf")

print("Success:", result.success)
print("Extractor used:", result.extractor_name)
print("Text:\n", result.text)

print("\nAttempt history:")
for attempt in result.attempts:
    print(
        attempt.extractor_name,
        "accepted:", attempt.accepted,
        "error:", attempt.error,
    )
