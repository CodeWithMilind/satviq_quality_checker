"""Combined Muse vision request — original document + Tika text +
PDF hyperlink annotations in ONE call, with the reconciliation
instructions; and usage/request-id capture. HTTP is faked at the
client seam; request shapes are asserted against the payload.
"""

import json
from pathlib import Path

import pytest

import muse_ai.client as client_module
from muse_ai import MuseConfig, MuseService

VALID = {"full_name": "Priya Nair", "skills": ["Python"]}
EXACT_URL = "https://example.com/portfolio?ref=resume%20v2"
LINKS = [{"page": 1, "url": EXACT_URL, "visible_text": "Portfolio",
          "type": "uri", "rect": [72.0, 100.0, 140.0, 122.0]}]


@pytest.fixture
def http(monkeypatch):
    calls = []

    def fake(url, payload, headers, timeout):
        calls.append(payload)
        return {"id": "req-abc-123", "model": "muse-spark-1.3",
                "choices": [{"message": {"content": json.dumps(VALID)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                          "total_tokens": 140,
                          "prompt_tokens_details": {"cached_tokens": 25}}}

    monkeypatch.setattr(client_module, "http_post_json", fake)
    return calls


def _service():
    return MuseService(MuseConfig(api_key="k", timeout_seconds=9.0))


def test_combined_request_contains_all_three_sources(http, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG-bytes")
    outcome = _service().vision_extract(
        png, tika_text="TIKA TEXT: Priya Nair, Python", hyperlinks=LINKS)
    assert outcome["ok"] is True
    assert outcome["visual_provided"] is True
    assert outcome["tika_text_provided"] is True
    assert outcome["tika_text_chars"] > 0
    assert outcome["hyperlinks_provided"] is True
    assert outcome["hyperlink_count"] == 1

    payload = http[0]
    developer = payload["messages"][0]["content"]
    assert "AUTHORITATIVE for URLs" in developer
    assert "Never invent" in developer
    parts = payload["messages"][1]["content"]
    kinds = [p["type"] for p in parts]
    assert kinds[0] == "text" and "image_url" in kinds
    texts = [p["text"] for p in parts if p["type"] == "text"]
    tika_part = next(t for t in texts if "TIKA EXTRACTED TEXT" in t)
    assert "Priya Nair, Python" in tika_part
    link_part = next(t for t in texts if "HYPERLINK ANNOTATIONS" in t)
    assert EXACT_URL in link_part  # preserved byte-for-byte
    assert '"visible_text": "Portfolio"' in link_part


def test_missing_tika_text_is_recorded_not_invented(http, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG-bytes")
    outcome = _service().vision_extract(png, tika_text=None,
                                        hyperlinks=None)
    assert outcome["ok"] is True
    assert outcome["visual_provided"] is True
    assert outcome["tika_text_provided"] is False
    assert outcome["hyperlinks_provided"] is False
    parts = http[0]["messages"][1]["content"]
    assert all("TIKA EXTRACTED TEXT" not in p.get("text", "")
               for p in parts)


def test_pdf_combined_request_uses_file_part(http):
    sample = Path(__file__).resolve().parent.parent / "sample_resume.pdf"
    outcome = _service().vision_extract(
        sample, tika_text="some tika text", hyperlinks=LINKS)
    assert outcome["ok"] is True
    assert outcome["input_type"] == "pdf"
    parts = http[0]["messages"][1]["content"]
    assert any(p["type"] == "file" for p in parts)


def test_usage_and_request_id_captured(http, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG")
    outcome = _service().vision_extract(png)
    assert outcome["request_id"] == "req-abc-123"
    assert outcome["usage_available"] is True
    assert outcome["usage"]["prompt_tokens"] == 100
    assert outcome["usage"]["prompt_tokens_details"]["cached_tokens"] == 25


def test_missing_usage_is_flagged_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(client_module, "http_post_json",
                        lambda *a, **k: {"choices": [{"message": {
                            "content": json.dumps(VALID)}}]})
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG")
    outcome = _service().vision_extract(png)
    assert outcome["ok"] is True
    assert outcome["usage_available"] is False
    assert outcome["usage"] == {}


def test_text_generation_also_receives_hyperlinks(http):
    outcome = _service().generate_resume_json(
        "Priya Nair — Portfolio", hyperlinks=LINKS)
    assert outcome["ok"] is True
    assert outcome["hyperlinks_provided"] is True
    user = http[0]["messages"][1]["content"]
    assert EXACT_URL in user
