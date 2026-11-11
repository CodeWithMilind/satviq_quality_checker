"""Unit tests for deterministic Indian mobile normalization.

Covers api/phone_numbers.py and its integration points: the Resume
JSON schema validator (normalization before the final JSON exists),
the store's read-time display normalization, and contact recovery's
recovered phone value. Extracted text is never modified — only the
structured phone field.
"""

import json

from api.phone_numbers import (
    is_valid_indian_mobile,
    normalize_indian_mobile,
    normalize_phone_field,
)
from muse_ai.schema import validate_resume_json

CANONICAL = "+91 9876543210"


# -- formats that must normalize -------------------------------------------

def test_bare_10_digit_mobile_gets_country_code():
    assert normalize_indian_mobile("9876543210") == CANONICAL


def test_spaced_number():
    assert normalize_indian_mobile("98765 43210") == CANONICAL


def test_hyphenated_number():
    assert normalize_indian_mobile("98765-43210") == CANONICAL


def test_bracketed_number():
    assert normalize_indian_mobile("(98765) 43210") == CANONICAL


def test_dotted_number():
    assert normalize_indian_mobile("98765.43210") == CANONICAL


def test_already_canonical_is_idempotent():
    assert normalize_indian_mobile(CANONICAL) == CANONICAL
    assert normalize_indian_mobile(normalize_indian_mobile("09876543210")) \
        == CANONICAL


# -- duplicate country code handling ----------------------------------------

def test_plus91_with_spaces_not_duplicated():
    assert normalize_indian_mobile("+91 98765 43210") == CANONICAL


def test_plus91_compact_not_duplicated():
    assert normalize_indian_mobile("+919876543210") == CANONICAL


def test_91_without_plus():
    assert normalize_indian_mobile("919876543210") == CANONICAL


def test_91_hyphenated():
    assert normalize_indian_mobile("91-98765-43210") == CANONICAL


def test_trunk_zero_prefix():
    assert normalize_indian_mobile("098765 43210") == CANONICAL


def test_091_international_trunk():
    assert normalize_indian_mobile("091 98765 43210") == CANONICAL


def test_each_mobile_series_start():
    for first in "6789":
        number = first + "876543210"
        assert normalize_indian_mobile(number) == f"+91 {number}"


# -- invalid / ambiguous values preserved verbatim --------------------------

def test_too_short_preserved():
    assert normalize_indian_mobile("98765") == "98765"


def test_too_long_preserved():
    assert normalize_indian_mobile("987654321098") == "987654321098"


def test_landline_preserved():
    # Pune landline: 020 + 8 digits — not a mobile, stays untouched.
    assert normalize_indian_mobile("020 4123 4567") == "020 4123 4567"


def test_10_digits_outside_mobile_series_preserved():
    # Starts with 4 — not an Indian mobile series.
    assert normalize_indian_mobile("4123456789") == "4123456789"


def test_foreign_number_preserved():
    assert normalize_indian_mobile("+1 415 555 0132") == "+1 415 555 0132"


def test_postal_code_untouched():
    # 6-digit PIN — far too short to be a phone; unchanged.
    assert normalize_indian_mobile("411028") == "411028"


def test_non_numeric_preserved():
    assert normalize_indian_mobile("N/A") == "N/A"
    assert normalize_indian_mobile("") == ""
    assert normalize_indian_mobile(None) == ""


def test_is_valid_helper():
    assert is_valid_indian_mobile("98765 43210") is True
    assert is_valid_indian_mobile("+91 9876543210") is True
    assert is_valid_indian_mobile("020 4123 4567") is False
    assert is_valid_indian_mobile("12345") is False


# -- multiple numbers in one field ------------------------------------------

def test_multiple_numbers_comma_separated():
    assert normalize_phone_field("9822012345, +91 98220 67890") == \
        "+91 9822012345, +91 9822067890"


def test_multiple_numbers_mixed_valid_and_invalid():
    # The valid mobile is normalized; the landline segment is kept.
    assert normalize_phone_field("9822012345 / 020 4123 4567") == \
        "+91 9822012345 / 020 4123 4567"


def test_single_value_field():
    assert normalize_phone_field("98220 12345") == "+91 9822012345"
    assert normalize_phone_field("") == ""


# -- integration: schema validation normalizes before final JSON ------------

def test_resume_json_phone_normalized_by_schema():
    payload = {"full_name": "Test Candidate",
               "phone": "+91-98220 12345",
               "email": "test@example.com"}
    parsed, error = validate_resume_json(json.dumps(payload))
    assert error is None
    assert parsed["phone"] == "+91 9822012345"
    # Other fields untouched.
    assert parsed["email"] == "test@example.com"


def test_resume_json_invalid_phone_preserved_by_schema():
    payload = {"full_name": "Test Candidate", "phone": "020 4123 4567"}
    parsed, error = validate_resume_json(json.dumps(payload))
    assert error is None
    assert parsed["phone"] == "020 4123 4567"


# -- integration: store read-time display normalization ----------------------

def test_store_public_normalizes_stored_phone():
    from api import store

    record = {"resume_id": "phone-test-0001",
              "resume_json": {"phone": "9822012345", "full_name": "X"}}
    served = store.public(record)
    assert served["resume_json"]["phone"] == "+91 9822012345"
    # The stored record itself is not rewritten by a read.
    assert record["resume_json"]["phone"] == "9822012345"


# -- integration: contact recovery carries canonical value -------------------

def test_contact_recovery_phone_is_canonical(tmp_path):
    from pipeline.contact_recovery import recover_contact_information

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-fake")
    results = [{"extractor": "docling",
                "text": "Soham Velhal\nPune, India\n"
                        "Call me on 98220 12345 anytime"}]
    _, block = recover_contact_information(
        normalized_text="Soham Velhal\nBody text without contacts.",
        selected_extractor="docling", results=results, file_path=pdf,
        text_layer_reader=lambda p: "", ocr_runner=lambda p: ("", None))
    assert block["recovered"]["phone"]["value"] == "+91 9822012345"
