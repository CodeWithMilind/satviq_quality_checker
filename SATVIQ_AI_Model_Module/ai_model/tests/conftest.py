"""Shared fixtures and a fake OpenAI client.

No test in this package makes a real API call: the OpenAI provider is
always constructed with an injected fake client, and configs use a
dummy key.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make `import ai_model` work no matter where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_model.config import AIModelConfig  # noqa: E402


def make_image_bytes(fmt: str = "PNG", size: tuple[int, int] = (120, 160)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color=(255, 255, 255)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture()
def png_bytes() -> bytes:
    return make_image_bytes("PNG")


@pytest.fixture()
def jpeg_bytes() -> bytes:
    return make_image_bytes("JPEG")


@pytest.fixture()
def test_config() -> AIModelConfig:
    return AIModelConfig(
        openai_api_key="sk-test-dummy-key",
        timeout_seconds=5,
        max_retries=2,
    )


def make_openai_response(content: str, *, total_tokens: int = 42) -> SimpleNamespace:
    """Minimal stand-in for an OpenAI ChatCompletion response."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=32, total_tokens=total_tokens
        ),
    )


class FakeCompletions:
    """Records create() calls; returns a canned response or raises a
    queued error (an exception instance, or a list of them consumed
    one per call)."""

    def __init__(self, *, content: str = "ok", error=None):
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.error, list):
            if self.error:
                raise self.error.pop(0)
        elif self.error is not None:
            raise self.error
        return make_openai_response(self.content)


class FakeModels:
    def __init__(self, error=None):
        self.error = error

    def list(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[])


class FakeOpenAIClient:
    def __init__(self, *, content: str = "ok", error=None, models_error=None):
        self.completions = FakeCompletions(content=content, error=error)
        self.chat = SimpleNamespace(completions=self.completions)
        self.models = FakeModels(error=models_error)


@pytest.fixture()
def fake_client_factory():
    return FakeOpenAIClient


VALID_RECOVERY_PAYLOAD = {
    "recovered_text": "Jane Doe\nPython Developer\nSkills: Python, FastAPI",
    "recovery_confidence": 0.92,
    "unreadable_sections": ["header logo"],
    "issues": ["tika output dropped the Skills section"],
}


@pytest.fixture()
def valid_payload_json() -> str:
    return json.dumps(VALID_RECOVERY_PAYLOAD)
