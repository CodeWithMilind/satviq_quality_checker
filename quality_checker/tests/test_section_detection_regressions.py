"""Regression coverage for heading-aware completeness detection."""

from quality_checker import QualityChecker
from quality_checker.filters.completeness import CompletenessFilter


def test_numbered_headings():
    result = CompletenessFilter().evaluate("01. EXPERIENCE\nEngineer at Acme\n02. EDUCATION\nBSc CS")
    assert {"Work Experience", "Education"} <= set(result.detected_sections)
    assert result.metrics["section_evidence"]["Work Experience"]["original_heading"] == "01. EXPERIENCE"
    assert result.metrics["section_evidence"]["Work Experience"]["line_number"] == 1


def test_numbered_sections_are_each_detected_when_other_is_missing():
    education_only = CompletenessFilter().evaluate("01. EDUCATION\nBachelor of Science in Computing")
    experience_only = CompletenessFilter().evaluate("01. EXPERIENCE\nSoftware Engineer at Acme")
    assert "Education" in education_only.detected_sections
    assert "Work Experience" in education_only.missing_sections
    assert "Work Experience" in experience_only.detected_sections
    assert "Education" in experience_only.missing_sections


def test_requested_variants():
    result = CompletenessFilter().evaluate("Professional Experience\nEngineer\nAcademic Qualifications\nBSc")
    assert {"Work Experience", "Education"} <= set(result.detected_sections)


def test_career_history_and_educational_background():
    result = CompletenessFilter().evaluate("Career History\nAnalyst\nEducational Background\nBCA")
    assert {"Work Experience", "Education"} <= set(result.detected_sections)


def test_extra_spaces_case_and_punctuation():
    result = CompletenessFilter().evaluate("  2 )   eDuCaTiOn :  \nBCA\nPROJECTS & ACHIEVEMENTS\nBuilt app")
    assert {"Education", "Projects"} <= set(result.detected_sections)


def test_heading_split_across_lines():
    result = CompletenessFilter().evaluate("Academic\nQualifications\nBSc at a recognised university")
    assert "Education" in result.detected_sections
    assert result.metrics["section_evidence"]["Education"]["detection_method"] == "joined_lines"


def test_experience_prose_does_not_create_section():
    result = CompletenessFilter().evaluate("Experience in Python, SQL and Machine Learning.\nBuilt several tools.")
    assert "Work Experience" in result.missing_sections


def test_qualification_content_is_not_work_experience():
    result = CompletenessFilter().evaluate("Qualification: BCA")
    assert "Work Experience" in result.missing_sections
    assert "Education" in result.detected_sections


def test_genuine_missing_sections_are_penalized():
    result = CompletenessFilter().evaluate("Summary\nA sufficiently long resume text without either target heading.")
    assert {"Work Experience", "Education"} <= set(result.missing_sections)
    assert result.score < 100


def test_poor_extraction_leaves_sections_uncertain():
    report = QualityChecker().check("broken �", document_id="d", source_tool="ocr", attempt_id="a")
    assert "Work Experience" in report.completeness.metrics["uncertain_sections"]
    assert "Work Experience" not in report.completeness.missing_sections
    assert "SECTION_PRESENCE_UNCERTAIN" in report.completeness.flags


def test_empty_poor_extraction_is_uncertain_not_missing():
    report = QualityChecker().check("", document_id="d", source_tool="ocr", attempt_id="a")
    assert report.completeness.missing_sections == []
    assert "Education" in report.completeness.metrics["uncertain_sections"]


def test_threshold_quality_score_is_uncertain():
    report = QualityChecker().check("short extracted text", document_id="d", source_tool="tika", attempt_id="a")
    assert report.text_health.score == 40
    assert "Work Experience" in report.completeness.metrics["uncertain_sections"]
    assert "Work Experience" not in report.completeness.missing_sections


def test_good_quality_text_with_missing_sections_is_penalized():
    text = "A readable resume body with detailed professional accomplishments and skills. " * 20
    report = QualityChecker().check(text, document_id="d", source_tool="tika", attempt_id="a")
    assert report.text_health.score == 100
    assert {"Work Experience", "Education"} <= set(report.completeness.missing_sections)
    assert not report.completeness.metrics["uncertain_sections"]
    assert report.completeness.score < 100


def test_optional_sections_keep_configured_low_weight():
    filt = CompletenessFilter(config={
        "sections": {
            "required": {"display_name": "Required", "importance": "required", "aliases": ["required"]},
            "recommended": {"display_name": "Recommended", "importance": "recommended", "aliases": ["recommended"]},
            "optional": {"display_name": "Optional", "importance": "optional", "aliases": ["optional"]},
        },
        "penalties_by_importance": {"required": 25, "recommended": 12, "optional": 3},
    })
    result = filt.evaluate("Required", extraction_quality=100)
    assert result.score == 85
    assert result.missing_required == []
    assert result.missing_recommended == ["Recommended"]
    assert result.missing_optional == ["Optional"]


def test_all_standard_sections_detected():
    text = """Contact Information\nname@example.com\nProfessional Summary\nExperienced developer\nSkills\nPython\nWork Experience\nEngineer\nEducation\nBSc\nProjects\nPortfolio\nCertifications\nCertificate"""
    result = CompletenessFilter().evaluate(text)
    assert {"Contact Information", "Summary", "Skills", "Work Experience", "Education", "Projects", "Certifications"} <= set(result.detected_sections)
    assert result.score == 100
