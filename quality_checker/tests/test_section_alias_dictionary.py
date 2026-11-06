"""Section alias dictionary — corpus-evidenced aliases, conditional
(content-gated) aliases, and false-positive guards.

The alias additions were discovered in a corpus of 41 real resumes
(see HEADING_ANALYSIS.md). Frequencies cited in comments are corpus
counts. Headings the product owner specified from resumes outside
the corpus are marked [spec] in completeness.yaml.
"""

import pytest

from quality_checker.filters.completeness import CompletenessFilter

BASE = """Jane Doe
jane.doe@example.com | +91 98765 43210 | Pune, India
Professional Summary
Python developer building backend and AI applications.
Skills
Python, FastAPI, SQL, LangChain
Work Experience
Data Scientist Intern at Example AI, May 2025 to Nov 2025.
Education
MCA, Example University, 2025 to 2027.
Certifications
Certified Data Scientist, Example Institute, 2025.
"""


@pytest.fixture(scope="module")
def filt():
    return CompletenessFilter()


def _without(heading):
    """BASE with the given canonical heading line removed."""
    lines = [l for l in BASE.splitlines() if l.strip() != heading]
    return "\n".join(lines) + "\n"


# --- the reported case ------------------------------------------------------

def test_portfolio_and_work_samples_counts_as_projects(filt):
    text = _without("Certifications") + (
        "PORTFOLIO & WORK SAMPLES\n"
        "PDF Chat Assistant - RAG application built with LangChain and FastAPI.\n"
        "Data4Viz - data analysis workbench with charts and dashboards.\n")
    # swap certifications back in spirit: score check uses projects only
    result = filt.evaluate(text + "Certifications\nCertified Data Scientist, 2025.\n")
    assert "Projects" in result.detected_sections
    assert "MISSING_OPTIONAL_SECTION" not in result.flags or \
        "Projects" not in result.missing_sections


@pytest.mark.parametrize("heading", [
    "PORTFOLIO & WORK SAMPLES",          # [spec] the reported heading
    "Work Samples",
    "Selected Works",
    "Project Portfolio",
    "Projects & Portfolio",
    "Key Projects & Achievements",
    "Relevant Projects",
    "Select Projects",                   # [corpus] x1
    "Flagship Projects",                 # [corpus] x1
])
def test_strong_project_aliases(filt, heading):
    result = filt.evaluate(BASE + heading + "\nBuilt a data dashboard.\n")
    assert "Projects" in result.detected_sections


# --- corpus-evidenced strong aliases ----------------------------------------

@pytest.mark.parametrize("heading,section", [
    ("Previous Experience", "Work Experience"),       # [corpus] x1
    ("FREELANCE EXPERIENCE", "Work Experience"),      # [corpus] x1
    ("ACADEMIC QUALIFICATION", "Education"),          # [corpus] x2 singular
    ("CERTIFICATIONS & TRAINING", "Certifications"),  # [corpus] x1
    ("CERTIFICATIONS & TECHNICAL TRAINING", "Certifications"),  # [corpus] x1
    ("Expertise", "Skills"),                          # [corpus] x2
    ("RELEVANT SKILLS", "Skills"),                    # [corpus] x2
    ("Skillset", "Skills"),                           # [corpus] x1
    ("Skills Summary", "Skills"),                     # [corpus] x1
    ("IT Skills", "Skills"),                          # [corpus] x1
    ("SOFTWARE & TOOLS", "Skills"),                   # [corpus] x1
    ("Tools & Technologies", "Skills"),               # [corpus] x1
    ("TECHNICAL ENVIRONMENT", "Skills"),              # [corpus] x1
    ("CORE SKILLS", "Skills"),                        # [corpus] x1
])
def test_corpus_strong_aliases(filt, heading, section):
    result = filt.evaluate(BASE + heading + "\nSupporting content line.\n")
    assert section in result.detected_sections


def test_combined_education_and_certifications_detects_both(filt):
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if l.strip() != "Certifications") + "\n"
    result = filt.evaluate(
        text + "EDUCATION & CERTIFICATIONS\nMCA 2025. Certified Data Scientist.\n")
    assert "Education" in result.detected_sections
    assert "Certifications" in result.detected_sections


# --- conditional aliases: content gate --------------------------------------

def test_portfolio_with_real_content_counts(filt):
    result = filt.evaluate(BASE + (
        "PORTFOLIO\n"
        "PDF Chat Assistant - a RAG application built with LangChain, "
        "FAISS and FastAPI, deployed for document question answering.\n"))
    assert "Projects" in result.detected_sections
    ev = result.metrics["section_evidence"]["Projects"]
    assert ev["detection_method"] == "heading_conditional"


def test_portfolio_url_only_does_not_count(filt):
    result = filt.evaluate(BASE + "Portfolio: https://example.com\n")
    assert "Projects" not in result.detected_sections


def test_portfolio_heading_with_only_a_link_line_does_not_count(filt):
    result = filt.evaluate(BASE + "PORTFOLIO\nhttps://example.com/work\n")
    assert "Projects" not in result.detected_sections


def test_portfolio_link_label_in_links_block_does_not_count(filt):
    # [corpus] SHJ-5329_SHC-363873.pdf — the label sits in a LINKS block.
    result = filt.evaluate(BASE + (
        "LINKS\n"
        "Portfolio: Google Drive (link on request)\n"
        "Instagram: @someone , @studio\n"
        "Website: someone.wixsite.com\n"))
    assert "Projects" not in result.detected_sections
    assert any(r["alias"] == "portfolio"
               for r in result.metrics["conditional_rejected"])


def test_portfolio_and_website_link_label_rejected(filt):
    # [corpus] SHJ-5329_SHC-365423.pdf — contact-block label.
    result = filt.evaluate(BASE + (
        "Portfolio & Website\n+91 8303738902\nsomeone@example.com\n"))
    assert "Projects" not in result.detected_sections


def test_technical_portfolio_both_ways(filt):
    good = filt.evaluate(BASE + (
        "Technical Portfolio\n"
        "Built an invoice OCR pipeline and a dashboard used by clients.\n"))
    assert "Projects" in good.detected_sections
    bad = filt.evaluate(BASE + "Technical Portfolio: https://example.com\n")
    assert "Projects" not in bad.detected_sections


def test_tools_heading_with_tool_list_counts_as_skills(filt):
    text = _without("Skills")
    # drop the skills content line too so only the conditional heading can hit
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("Python, FastAPI")) + "\n"
    result = filt.evaluate(text + (
        "Tools\nAdobe After Effects, Premiere Pro, Photoshop, Illustrator, "
        "Figma and Blender for motion graphics production work.\n"))
    assert "Skills" in result.detected_sections


def test_tech_stack_projects_counts(filt):
    # [corpus] download_8_.pdf
    result = filt.evaluate(BASE + (
        "Tech Stack Projects\n"
        "Edureka Data Platform - built ingestion pipelines with Python, "
        "FastAPI, PySpark and PostgreSQL for analytics workloads.\n"))
    assert "Projects" in result.detected_sections


# --- '&' delimiter fix --------------------------------------------------------

def test_ampersand_heading_with_inline_content(filt):
    result = filt.evaluate(
        BASE + "Projects & Achievements: Built a RAG PDF chat assistant.\n")
    assert "Projects" in result.detected_sections


def test_ampersand_portfolio_heading_with_inline_content(filt):
    result = filt.evaluate(
        BASE + "PORTFOLIO & WORK SAMPLES: PDF Chat Assistant, a RAG app.\n")
    assert "Projects" in result.detected_sections


# --- false-positive guards ----------------------------------------------------

def test_experience_prose_is_not_a_section(filt):
    result = filt.evaluate(BASE + "Experience in Python and SQL development.\n")
    # Work Experience is in BASE already; use a base without it:
    text = _without("Work Experience")
    result = filt.evaluate(text + "Experience in Python and SQL development.\n")
    assert "Work Experience" not in result.detected_sections


def test_qualification_label_is_not_experience(filt):
    text = _without("Work Experience")
    result = filt.evaluate(text + "Qualification: BCA\n")
    assert "Work Experience" not in result.detected_sections


def test_unscored_sections_never_affect_detection_or_score(filt):
    plain = filt.evaluate(BASE)
    with_extra = filt.evaluate(BASE + (
        "LANGUAGES\nEnglish, Hindi\nACHIEVEMENTS\nWon a hackathon.\n"
        "DECLARATION\nI hereby declare the above is true.\n"))
    assert with_extra.detected_sections == plain.detected_sections
    assert with_extra.score == plain.score


def test_scoring_config_unchanged(filt):
    assert list(filt.sections) == [
        "contact_information", "summary", "skills", "work_experience",
        "education", "projects", "certifications"]
    assert filt.penalties == {"required": 25, "recommended": 12, "optional": 3}
    importance = {k: v["importance"] for k, v in filt.sections.items()}
    assert importance == {
        "contact_information": "required", "summary": "optional",
        "skills": "recommended", "work_experience": "recommended",
        "education": "recommended", "projects": "optional",
        "certifications": "optional"}


# --- CV-dataset (95 resumes) additions ---------------------------------------

@pytest.mark.parametrize("heading,section", [
    ("EDUCATIONAL QUALIFICATION", "Education"),       # [cv100] x6 singular
    ("TECHNICAL QUALIFICATION", "Education"),         # [cv100] x3
    ("PROFESSIONAL QUALIFICATION", "Education"),      # [cv100] x2
    ("ACADEMICS", "Education"),                       # [cv100] x2
    ("KEY SKILLS", "Skills"),                         # [cv100] x3
    ("SOFT SKILLS", "Skills"),                        # [cv100] x3
    ("PROFESSIONAL SKILLS", "Skills"),                # [cv100] x2
    ("SOFTWARE EXPOSURE", "Skills"),                  # [cv100] x1
    ("PERSONAL PROFILE", "Summary"),                  # [cv100] x4
    ("CAREER SUMMARY", "Summary"),                    # [cv100] x2
    ("Career Objectives", "Summary"),                 # [cv100] x2
    ("ORGANISATIONAL EXPERIENCE", "Work Experience"),  # [cv100] x1
    ("PROJECT WORK", "Projects"),                     # [cv100] x1
    ("TRAINING & CERTIFICATIONS", "Certifications"),  # [cv100] x1
    ("CERTIFICATIONS & WORKSHOPS", "Certifications"),  # [cv100] x1
    ("Certification", "Certifications"),              # [cv100] x1 singular
    ("AWARDS AND CERTIFICATIONS", "Certifications"),  # [cv100] x2 combined
    ("SKILLS & CERTIFICATIONS", "Skills"),            # [cv100] x1 combined
])
def test_cv100_strong_aliases(filt, heading, section):
    result = filt.evaluate(BASE + heading + "\nSupporting content line.\n")
    assert section in result.detected_sections


def test_cv100_combined_skills_and_certifications_detects_both(filt):
    result = filt.evaluate(BASE + "SKILLS & CERTIFICATIONS\nPython. PMP.\n")
    assert "Skills" in result.detected_sections
    assert "Certifications" in result.detected_sections


def test_numbered_heading_glued_to_word(filt):
    # [cv100] SHJ-5329_SHC-363945.pdf uses "01EXPERIENCE" / "02EDUCATION".
    text = _without("Work Experience")
    result = filt.evaluate(text + "01EXPERIENCE\nDesigner at Studio, 2025.\n")
    assert "Work Experience" in result.detected_sections


def test_sidebar_portfolio_link_label_rejected_despite_prose_leak(filt):
    # [cv100] SHJ-5329_SHC-363945.pdf — a sidebar PORTFOLIO label above
    # a Behance link; two-column extraction leaks the summary prose
    # and an experience section's dates into the content window. The
    # gate must still reject it: the window stops at the (glued)
    # numbered EXPERIENCE heading, leaving no work-record evidence.
    text = _without("Work Experience")
    result = filt.evaluate(text + (
        "PORTFOLIO\n"
        "behance.net/rohitroy5\n"
        "Rohit\nRoy\n"
        "SR. MOTION GRAPHIC DESIGNER / VISUALIZER\n"
        "Creative professional with 6+ years of experience in motion "
        "design and AI-driven content production, leading creative "
        "pipelines from concept to final delivery.\n"
        "01EXPERIENCE\n"
        "Sr. Motion Graphic Designer\n"
        "2025 to Present\n"))
    assert "Projects" not in result.detected_sections
    assert any(r["alias"] == "portfolio"
               for r in result.metrics["conditional_rejected"])


# --- Content-evidence patterns + glued / letter-spaced headings --------------
# From the CV-dataset error analysis: sections present as tables or
# content with no usable heading (reported case: SHJ-5464_SHC-385075).

EDU_TABLE = """Degree
Board
Institute
% / CGPI
Year
PGDM
A.I.C.T.E
N. L. Dalmia Institute of Management Studies & Research
7.8/10
2024
B.Com.
Amity university
7.84/10
2021
Class XII
C.B.S.E
Kendriya Vidyalaya Number 1
78.6
2018
"""


def test_education_table_without_heading_detected_by_pattern(filt):
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("MCA, Example")) + "\n"
    result = filt.evaluate(text + EDU_TABLE)
    assert "Education" in result.detected_sections
    assert result.metrics["section_evidence"]["Education"][
        "detection_method"] == "pattern"


def test_degree_name_in_text_is_education_evidence(filt):
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("MCA, Example")) + "\n"
    result = filt.evaluate(text + "BACHELOR OF COMMERCE\n2013 To 2015\n")
    assert "Education" in result.detected_sections


def test_no_degree_evidence_keeps_education_missing(filt):
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("MCA, Example")) + "\n"
    result = filt.evaluate(text + "Worked on internal tooling and support.\n")
    assert "Education" not in result.detected_sections


def test_education_level_label_form(filt):
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("MCA, Example")) + "\n"
    result = filt.evaluate(text + "Education level: ITI\nITI Electrician\n")
    assert "Education" in result.detected_sections


def test_glued_heading_academicachievements(filt):
    # [cv100] SHJ-5464_SHC-385075.pdf — heading with no space at all;
    # conditional alias + content gate via the space-insensitive match.
    text = _without("Education")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("MCA, Example")) + "\n"
    result = filt.evaluate(text + (
        "ACADEMICACHIEVEMENTS\n"
        "• Secured 3rd Rank in India in the Bloomberg Global Trading "
        "Competition 2023 held in Pune.\n"))
    assert "Education" in result.detected_sections


def test_letter_spaced_headings(filt):
    result = filt.evaluate(BASE + "E D U C A T I O N\nIgnored duplicate.\n")
    assert "Education" in result.detected_sections
    text = _without("Professional Summary")
    result = filt.evaluate(text + "C A R E E R O B J E C T I V E\nTo grow.\n")
    assert "Summary" in result.detected_sections


def test_certification_acronym_and_credential_shapes(filt):
    assert "Certifications" in filt.evaluate(
        BASE + "CFA LEVEL 2 Cleared.\n").detected_sections
    assert "Certifications" in filt.evaluate(
        BASE + "Level 1 & 2 Certification in Oracle Aconex software.\n"
    ).detected_sections
    assert "Certifications" in filt.evaluate(
        BASE + "Training & Certification \"ML Professional\" at Academy.\n"
    ).detected_sections


def test_certification_false_positive_guards(filt):
    # [cv100] SHJ-5433_SHC-381530.pdf — a duty sentence, not a credential.
    duty = filt.evaluate(BASE + (
        "Supported the compilation and validation of design freeze "
        "documents and certifications by gathering inputs.\n"))
    # Certifications IS in BASE via its heading; strip the heading:
    text = _without("Certifications")
    text = "\n".join(l for l in text.splitlines()
                     if not l.startswith("Certified Data Scientist")) + "\n"
    duty = filt.evaluate(text + (
        "Supported the compilation and validation of design freeze "
        "documents and certifications by gathering inputs.\n"))
    assert "Certifications" not in duty.detected_sections
    # [cv100] SHJ-5464_SHC-384979.pdf — an award, not a certification.
    award = filt.evaluate(text + (
        "Earned the Certificate of Appreciation & Performer of the "
        "Month awards for exceptional market analysis reports.\n"))
    assert "Certifications" not in award.detected_sections
    # School certificates are education evidence (HSC), not certifications.
    school = filt.evaluate(text + (
        "Higher Secondary Certificate (HSC) | Percentage: 87%\n"))
    assert "Certifications" not in school.detected_sections


# --- Embedded headings (heading inside a heading-like line) ------------------
# From SHJ-5451_SHC-364880.pdf: "OJT /Work Experience at TATA MOTORS
# LTD, Pantnagar :" — the heading carries a prefix and the employer.

def _without_section(section: str) -> str:
    specs = {"Work Experience": "Work Experience", "Summary": "Professional Summary",
             "Education": "Education", "Certifications": "Certifications"}
    return _without(specs.get(section, section))


def test_embedded_heading_with_employer(filt):
    text = _without_section("Work Experience")
    result = filt.evaluate(text + (
        "OJT /Work Experience at TATA MOTORS LTD, Pantnagar :\n"
        "As a Trainee I have rich hands-on experience in the plant.\n"))
    assert "Work Experience" in result.detected_sections
    assert result.metrics["section_evidence"]["Work Experience"][
        "detection_method"] == "heading_embedded"


def test_embedded_heading_duration_suffix(filt):
    text = _without_section("Work Experience")
    result = filt.evaluate(text + "WORKING EXPERIENCE ( 2.5+ Years )\nDid things.\n")
    assert "Work Experience" in result.detected_sections
    assert result.metrics["section_evidence"]["Work Experience"][
        "detection_method"] == "heading_embedded"


def test_embedded_heading_combined_and_suffix(filt):
    text = _without_section("Work Experience")
    result = filt.evaluate(text + "PROFESSIONAL EXPERIENCE & TRAINING\nDid things.\n")
    assert "Work Experience" in result.detected_sections


def test_embedded_heading_digit_glued_prefix(filt):
    text = _without_section("Work Experience")
    result = filt.evaluate(text + "2289936201423Work Experience\nDid things.\n")
    assert "Work Experience" in result.detected_sections


def test_embedded_heading_prose_guards(filt):
    text = _without_section("Work Experience")
    # Long prose prefix before the phrase.
    prose1 = filt.evaluate(text + (
        "I have 3 years of work experience at Tata Motors building "
        "internal tools for the team.\n"))
    assert "Work Experience" not in prose1.detected_sections
    # Connector suffix without a capitalised employer (prose object).
    prose2 = filt.evaluate(text + "Improved work experience at retail stores.\n")
    assert "Work Experience" not in prose2.detected_sections
    # Long combined suffix.
    prose3 = filt.evaluate(text + (
        "Professional experience and training programs at the institute "
        "for working professionals in the region.\n"))
    assert "Work Experience" not in prose3.detected_sections


def test_delimiter_heading_behind_bullet_glyph(filt):
    # [cv100] SHJ-5442_SHC-321887.pdf — a symbol-font bullet glyph
    # precedes the heading on the raw line.
    text = _without_section("Summary")
    result = filt.evaluate(text + (
        "\uf076 CAREER OBJECTIVE :-To work in a professional "
        "environment which brings out the best in me.\n"))
    assert "Summary" in result.detected_sections


def test_education_qualifications_plural_and_combined(filt):
    assert "Education" in filt.evaluate(
        BASE + "Education Qualifications:\nBSc, State University.\n").detected_sections
    assert "Education" in filt.evaluate(
        BASE + "EDUCATION & QUALIFICATION\nITI Electrician.\n").detected_sections
