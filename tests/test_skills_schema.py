"""Skills schema validation tests.

Muse AI returns skills categorized as an object; ResumeJSON used to
expect a flat list, which failed validation with "Input should be a
valid list / input_type=dict". These tests pin the fix: the nested
Skills model, empty-list defaults, and backward compatibility with
the flat-list form produced before categorization.
"""

import json

from muse_ai.schema import ResumeJSON, Skills, validate_resume_json

CATEGORIZED = {
    "technical_skills": ["Python", "Machine Learning"],
    "soft_skills": ["Communication"],
    "tools": ["Docker"],
    "taxonomy_skills": [],
    "other_skills": [],
}

EMPTY_SKILLS = {
    "technical_skills": [],
    "soft_skills": [],
    "tools": [],
    "taxonomy_skills": [],
    "other_skills": [],
}


def test_categorized_skills_object_validates():
    parsed, error = validate_resume_json(
        json.dumps({"full_name": "Priya Nair", "skills": CATEGORIZED}))
    assert error is None
    assert parsed["skills"] == CATEGORIZED


def test_missing_categories_default_to_empty_lists():
    parsed, error = validate_resume_json(
        '{"skills": {"technical_skills": ["Python"]}}')
    assert error is None
    assert parsed["skills"] == {**EMPTY_SKILLS,
                                 "technical_skills": ["Python"]}


def test_missing_skills_defaults_to_empty_categories():
    parsed, error = validate_resume_json('{"full_name": "Priya Nair"}')
    assert error is None
    assert parsed["skills"] == EMPTY_SKILLS


def test_flat_list_skills_backward_compatible():
    """Pre-categorization outputs (and older stored records) carry a
    flat list; every item must survive the conversion."""
    parsed, error = validate_resume_json(
        '{"skills": ["Python", "FastAPI", "Docker"]}')
    assert error is None
    assert parsed["skills"]["technical_skills"] == [
        "Python", "FastAPI", "Docker"]
    assert parsed["skills"]["other_skills"] == []


def test_skills_model_direct():
    skills = Skills(technical_skills=["Python"])
    assert skills.soft_skills == []
    assert skills.tools == []
    assert skills.taxonomy_skills == []
    assert skills.other_skills == []
    assert ResumeJSON().skills == Skills()


def test_category_as_single_string_is_wrapped():
    parsed, error = validate_resume_json(
        '{"skills": {"tools": "Docker"}}')
    assert error is None
    assert parsed["skills"]["tools"] == ["Docker"]


def test_unknown_category_preserved_in_other_skills():
    parsed, error = validate_resume_json(
        '{"skills": {"programming_languages": ["Go"]}}')
    assert error is None
    assert parsed["skills"]["other_skills"] == ["Go"]


def test_generated_json_schema_describes_skills_object():
    """The Muse system prompt embeds this generated schema, so the
    model is now instructed to return the categorized object."""
    schema = ResumeJSON.model_json_schema()
    assert "Skills" in schema["$defs"]
    properties = schema["$defs"]["Skills"]["properties"]
    assert set(properties) == set(CATEGORIZED)
