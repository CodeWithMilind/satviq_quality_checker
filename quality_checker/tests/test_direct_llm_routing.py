"""Direct-LLM routing (Approach A): detection-gap promotion.

When the extractor chain leaves a record unselected even though an
attempt produced readable text, the sections completeness missed at
required/recommended importance are probed against the candidate's
own text (api/content_probe.py, config in completeness.yaml's
content_probes block). Content present -> the miss was a
heading-detection gap and the record is forwarded to the LLM text
stage; content absent -> the record stands. Corrupt text (Text
Health below the gate's fail_below) is never promoted.
"""

from pathlib import Path

from api import pipeline
from api.content_probe import load_probes, probe_sections

# Ranjeet Kumar's resume text (the motivating example): the content
# is all present, but the headings are typo'd/glued — WORKEXPRIENCE,
# HANDLINGEQUIPMENT, ACADAMICQUALIFICATION — so completeness detects
# only Contact Information and Education.
RANJEET_TEXT = """CURRICULUMVITAE
RANJEET KUMAR
Mob:+91-8949239107 Email:ranjeetkumarverma555@gmail.com
CAREEROBJECTIVE:
I desire to work in competitive environment and challenging
assignments. This will enable me to leverage my skills and abilities.
WORKEXPRIENCE:
2 YEAR EXP. IN DCM CHEMICAL FARTILIZER IN PVC PLANT
WORKING WITH Otsuka Chemical India Pvt. Ltd. Keshwana, Kotputli
Join 01/01/2024 TO TILL DATE
RESPONSIBILITIES:
Maintain House Keeping of all over plant. Handled 07 Manpower.
HANDLINGEQUIPMENT:
Top Discharge Centrifuge, Vacuum Pump, Centrifugal Pump etc.
ACADAMICQUALIFICATION:
10th from BSER Ajmer in 2013
TECHNICALQUALIFICATION:
ITI COE Chemical from DTE Jodhpur in 2015 with 68.00%
DECLARATION:
I declare that all the above information is true.
"""

NO_EXPERIENCE_TEXT = """JANE DOE
jane@example.com +91 9000000000
OBJECTIVE: Seeking an entry-level design role.
EDUCATION: B.Sc, City University, 2024.
HOBBIES: Sketching and photography.
"""


def _payload(text_health=93.0, completeness=40.0, reading_order=100.0,
             missing_recommended=("Work Experience", "Skills")):
    return {
        "text_health": {"score": text_health},
        "completeness": {
            "score": completeness,
            "detected_sections": ["Contact Information", "Education"],
            "missing_sections": list(missing_recommended),
            "missing_required": [],
            "missing_recommended": list(missing_recommended),
            "missing_optional": ["Projects", "Certifications"],
        },
        "reading_order": {"score": reading_order},
    }


def _dead_record(text=RANJEET_TEXT, **payload_kwargs):
    """A record in the shape build_record_from_result leaves when the
    chain selected nothing: success False, no record text, one
    readable but rejected attempt."""
    payload = _payload(**payload_kwargs)
    return {
        "resume_id": "r-route",
        "success": False,
        "extracted_text": "",
        "final_extracted_text": "",
        "extractor_used": None,
        "final_extractor": None,
        "quality": None,
        "quality_status": "FAIL",
        "final_quality_status": "FAIL",
        "error": "No acceptable extraction.",
        "selection_reason": "No candidate was selected.",
        "attempts": [{
            "extractor_name": "tika",
            "success": True,
            "extracted_text": text,
            "normalized_text": text,
            "quality_status": "FAIL",
            "quality": payload,
            "quality_checker_report": payload,
            "selected": False,
            "is_final_selected": False,
            "decision": "reject",
        }],
        "pipeline_trace": [
            {"stage": "Final Result", "type": "final", "status": "FAIL",
             "message": "No candidate was selected.", "extractor": None,
             "accepted": False, "selected": False},
        ],
    }


# --- probe layer -----------------------------------------------------------

def test_probe_config_loads_from_completeness_yaml():
    probes = load_probes()
    for section in ("Work Experience", "Skills", "Education",
                    "Contact Information", "Summary", "Projects",
                    "Certifications"):
        assert section in probes


def test_probe_finds_content_behind_typo_and_glued_headings():
    probed = probe_sections(
        RANJEET_TEXT,
        ["Work Experience", "Skills", "Projects", "Certifications"])
    assert probed["Work Experience"]["present"] is True
    assert probed["Skills"]["present"] is True
    # Genuinely absent sections stay absent.
    assert probed["Projects"]["present"] is False
    assert probed["Certifications"]["present"] is False


def test_probe_education_behind_glued_typo_heading():
    probed = probe_sections(RANJEET_TEXT, ["Education"])
    assert probed["Education"]["present"] is True


def test_probe_negative_when_content_genuinely_absent():
    probed = probe_sections(NO_EXPERIENCE_TEXT, ["Work Experience"])
    assert probed["Work Experience"]["present"] is False


# --- routing ---------------------------------------------------------------

def test_detection_gap_record_is_promoted():
    record = _dead_record()
    routing = pipeline._apply_direct_llm_routing(record)
    assert routing is not None and routing["routed"] is True
    assert routing["reason"] == "detection_gap"
    assert routing["candidate_extractor"] == "tika"
    assert set(routing["trigger_sections"]) == {"Work Experience",
                                                "Skills"}
    assert record["success"] is True
    assert record["extractor_used"] == "tika"
    assert record["extracted_text"] == RANJEET_TEXT
    assert record["final_extracted_text"] == RANJEET_TEXT
    assert record["error"] is None
    attempt = record["attempts"][0]
    assert attempt["selected"] is True
    assert attempt["is_final_selected"] is True
    # History is not rewritten: the attempt's gate decision stands.
    assert attempt["decision"] == "reject"
    assert "Direct-LLM routing" in record["selection_reason"]
    stages = [s["stage"] for s in record["pipeline_trace"]]
    assert any("Direct LLM Routing" in s for s in stages)


def test_genuinely_absent_content_is_not_routed():
    record = _dead_record(text=NO_EXPERIENCE_TEXT)
    assert pipeline._apply_direct_llm_routing(record) is None
    assert record["success"] is False
    assert "direct_llm_routing" not in record


def test_corrupt_text_is_never_promoted():
    # Text Health below the gate's fail_below: garbage stays blocked
    # even when probe stems happen to appear in it.
    record = _dead_record(text_health=30.0)
    assert pipeline._apply_direct_llm_routing(record) is None
    assert record["success"] is False


def test_successful_record_is_untouched():
    record = _dead_record()
    record["success"] = True
    record["extracted_text"] = RANJEET_TEXT
    assert pipeline._apply_direct_llm_routing(record) is None
    assert "direct_llm_routing" not in record


def test_optional_only_misses_do_not_trigger():
    record = _dead_record(missing_recommended=())
    assert pipeline._apply_direct_llm_routing(record) is None


# --- interaction with the Muse stages --------------------------------------

class _FakeService:
    provider_name = "Muse AI"
    model = "muse-spark-1.3"
    configured = True
    vision_enabled = True
    configuration_error = None
    config = type("Cfg", (), {"timeout_seconds": 20.0,
                              "context_window_tokens": None})()

    def __init__(self):
        self.vision_calls = 0
        self.json_calls = 0

    def vision_extract(self, path, **kwargs):
        self.vision_calls += 1
        raise AssertionError("vision must be skipped for routed records")

    def generate_resume_json(self, text, **kwargs):
        self.json_calls += 1
        return {"ok": True, "error": None, "error_type": None,
                "resume_json": {"full_name": "Ranjeet Kumar"},
                "validation": "valid", "provider": "Muse AI",
                "model": "muse-spark-1.3", "raw_text": "{}",
                "usage": {"total_tokens": 10}, "duration_ms": 5.0,
                "timeout_seconds": 20.0, "input_type": "text"}


def test_routed_record_goes_straight_to_text_stage(tmp_path):
    record = _dead_record()
    pipeline._apply_direct_llm_routing(record)
    assert record["success"] is True
    doc = tmp_path / "resume.txt"
    doc.write_text(RANJEET_TEXT, encoding="utf-8")
    service = _FakeService()
    pipeline._apply_muse_stages(record, doc, service, None)
    assert service.vision_calls == 0      # vision skipped
    assert service.json_calls == 1        # text stage ran
    assert record["resume_json"] == {"full_name": "Ranjeet Kumar"}
    assert record["resume_json_source"] == "muse_ai"
    # No muse_ai vision attempt was appended.
    assert all(a["extractor_name"] != "muse_ai"
               for a in record["attempts"])
