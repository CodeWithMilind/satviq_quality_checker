"""Muse AI client — Meta Model API, Chat Completions surface.

Protocol verified against the official documentation
(https://dev.meta.ai/docs/overview and the Chat Completions /
Image understanding / File handling guides):

- ``POST {base_url}/chat/completions`` with ``Authorization:
  Bearer <MUSE_API_KEY>``; body carries ``model`` + ``messages``.
- The reply text lives at ``choices[0].message.content``; token
  usage at ``usage`` (prompt/completion/total tokens).
- Optional parameters are sent ONLY when configured:
  ``max_completion_tokens`` (the documented output-cap parameter)
  and ``temperature`` (documented range 0–2).
- Images go in as ``image_url`` content parts whose ``url`` is a
  base64 ``data:`` URL.
- PDFs go in as ``file`` content parts whose nested ``file`` object
  carries ``file_data`` (base64, as a data URL) and ``filename``;
  the API converts the PDF server-side (text from the first 100
  pages, per-page images for the first 50).

The HTTP call sits behind :func:`http_post_json` so tests can fake
it. API keys are scrubbed from every error message.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request

from .config import MuseConfig


class MuseError(Exception):
    """Base error for Muse API failures (never contains the key)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class MuseConfigurationError(MuseError):
    """Missing/invalid backend configuration."""


class MuseAuthError(MuseError):
    """The API rejected the configured key (HTTP 401/403)."""


class MuseTimeoutError(MuseError):
    """The transport reported a timeout (kept for classification;
    requests are sent with no configured timeout)."""


def _timeout_message(timeout: float | None) -> str:
    if timeout is None:
        return "Muse API request timed out."
    return f"Muse API request timed out after {timeout:g}s."


def http_post_json(url: str, payload: dict, headers: dict,
                   timeout: float | None) -> dict:
    """POST JSON and parse the JSON reply (test seam).

    ``timeout=None`` means NO request timeout: urlopen is called
    without a timeout argument and the call waits for the API
    however long it takes (timeouts were removed by request)."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        if timeout is None:
            response_cm = urllib.request.urlopen(request)
        else:
            response_cm = urllib.request.urlopen(request, timeout=timeout)
        with response_cm as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        raise MuseError(
            f"Muse API returned HTTP {exc.code}: {body}",
            status=exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MuseTimeoutError(_timeout_message(timeout)) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise MuseTimeoutError(_timeout_message(timeout)) from exc
        raise MuseError(
            f"Could not reach the Muse API: {exc.reason}") from exc


class MuseClient:
    """Thin Chat Completions client bound to one MuseConfig."""

    def __init__(self, config: MuseConfig):
        self.config = config

    # -- internals -----------------------------------------------------
    def _require_key(self) -> str:
        if not self.config.configured:
            raise MuseConfigurationError(
                self.config.configuration_error
                or "Muse AI is not configured.")
        return self.config.api_key or ""

    def _scrub(self, text: str) -> str:
        key = self.config.api_key
        return text.replace(key, "***") if key else text

    def _payload(self, messages: list[dict]) -> dict:
        payload: dict = {"model": self.config.model,
                         "messages": messages}
        if self.config.max_output_tokens is not None:
            payload["max_completion_tokens"] = \
                self.config.max_output_tokens
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        return payload

    def _send(self, messages: list[dict]) -> dict:
        key = self._require_key()
        url = f"{self.config.base_url}/chat/completions"
        started = time.perf_counter()
        try:
            data = http_post_json(
                url, self._payload(messages),
                {"Authorization": f"Bearer {key}"},
                timeout=self.config.timeout_seconds)
        except MuseError as exc:
            if exc.status in (401, 403):
                raise MuseAuthError(self._scrub(str(exc))) from exc
            exc.args = (self._scrub(str(exc)),)
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise MuseError(
                "Muse API reply had no message content.") from exc
        return {"text": text, "model": data.get("model"),
                "usage": data.get("usage") or {},
                "request_id": data.get("id"),
                "duration_ms": duration_ms}

    # -- public calls ----------------------------------------------------
    def chat_text(self, instructions: str, user_text: str) -> dict:
        """One text completion (developer instructions + user text)."""
        return self._send([
            {"role": "developer", "content": instructions},
            {"role": "user", "content": user_text}])

    @staticmethod
    def text_part(text: str) -> dict:
        return {"type": "text", "text": text}

    @staticmethod
    def image_part(blob: bytes, mime: str) -> dict:
        data_url = (f"data:{mime};base64,"
                    + base64.b64encode(blob).decode("ascii"))
        return {"type": "image_url", "image_url": {"url": data_url}}

    @staticmethod
    def pdf_part(pdf_bytes: bytes, filename: str) -> dict:
        data_url = ("data:application/pdf;base64,"
                    + base64.b64encode(pdf_bytes).decode("ascii"))
        return {"type": "file",
                "file": {"file_data": data_url, "filename": filename}}

    def chat_parts(self, instructions: str,
                   content_parts: list[dict]) -> dict:
        """One completion whose user message is a list of content
        parts (text / image_url / file, in the given order)."""
        return self._send([
            {"role": "developer", "content": instructions},
            {"role": "user", "content": content_parts}])

    def chat_images(self, instructions: str, prompt: str,
                    images: list[tuple[bytes, str]]) -> dict:
        """Vision completion: prompt + images as data-URL parts."""
        parts = [self.text_part(prompt)]
        parts += [self.image_part(blob, mime) for blob, mime in images]
        return self.chat_parts(instructions, parts)

    def chat_pdf(self, instructions: str, prompt: str, pdf_bytes: bytes,
                 filename: str) -> dict:
        """Document completion: the ORIGINAL PDF as a file part."""
        return self.chat_parts(
            instructions,
            [self.text_part(prompt), self.pdf_part(pdf_bytes, filename)])
