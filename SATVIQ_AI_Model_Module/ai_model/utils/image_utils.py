"""Resume page-image handling for the ``ai_model`` module.

The existing Extractor produces page images; this module only
validates and prepares them for a vision model:

- PNG and JPEG only.
- Format is detected from magic bytes, never from a filename.
- Oversized pages are downscaled and, if still too large, re-encoded
  as JPEG so unnecessarily large images are never sent.
- Pages are processed independently and keep their page index.

No PDF rendering or extraction happens here.
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..exceptions import ImageProcessingError

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

MEDIA_TYPE_PNG = "image/png"
MEDIA_TYPE_JPEG = "image/jpeg"

DEFAULT_MAX_DIMENSION = 2000
DEFAULT_MAX_BYTES = 4_000_000


@dataclass(frozen=True)
class PreparedImage:
    """One validated, provider-ready page image."""

    page_index: int
    media_type: str
    data: bytes
    width: int
    height: int

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @property
    def data_uri(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"


def detect_media_type(data: bytes) -> str:
    """Return 'image/png' or 'image/jpeg' from magic bytes."""
    if data.startswith(_PNG_MAGIC):
        return MEDIA_TYPE_PNG
    if data.startswith(_JPEG_MAGIC):
        return MEDIA_TYPE_JPEG
    raise ImageProcessingError(
        "Unsupported image format: only PNG and JPEG page images are supported."
    )


def _load_pillow():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImageProcessingError(
            "Pillow is required for image processing. Install it with "
            "`pip install Pillow` (see requirements.txt)."
        ) from exc
    return Image


def _coerce_to_bytes(image: Any, page_index: int) -> bytes:
    """Accept bytes, a PreparedImage, a path, a data URI, or a base64
    string and return raw image bytes."""
    if isinstance(image, PreparedImage):
        return image.data
    if isinstance(image, (bytes, bytearray)):
        raw = bytes(image)
    elif isinstance(image, (str, os.PathLike)):
        text = os.fspath(image)
        if text.startswith("data:image/"):
            _, _, payload = text.partition(",")
            try:
                raw = base64.b64decode(payload, validate=True)
            except Exception as exc:
                raise ImageProcessingError(
                    f"Page {page_index}: invalid base64 data URI."
                ) from exc
        elif Path(text).is_file():
            raw = Path(text).read_bytes()
        else:
            try:
                raw = base64.b64decode(text, validate=True)
            except Exception as exc:
                raise ImageProcessingError(
                    f"Page {page_index}: string image is neither an existing "
                    "file path, a data URI, nor valid base64."
                ) from exc
    else:
        raise ImageProcessingError(
            f"Page {page_index}: unsupported image input type "
            f"{type(image).__name__}; expected bytes, base64 string, "
            "data URI, or file path."
        )
    if not raw:
        raise ImageProcessingError(f"Page {page_index}: image is empty.")
    return raw


def prepare_image(
    image: Any,
    page_index: int = 0,
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PreparedImage:
    """Validate one page image and shrink it if it is unnecessarily
    large. Raises :class:`ImageProcessingError` on any problem."""
    raw = _coerce_to_bytes(image, page_index)
    try:
        media_type = detect_media_type(raw)
    except ImageProcessingError as exc:
        raise ImageProcessingError(f"Page {page_index}: {exc}") from exc

    Image = _load_pillow()
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            width, height = img.size
            needs_resize = max(width, height) > max_dimension
            needs_shrink = len(raw) > max_bytes
            if not needs_resize and not needs_shrink:
                return PreparedImage(page_index, media_type, raw, width, height)

            work = img.convert("RGB")
            if needs_resize:
                work.thumbnail((max_dimension, max_dimension))
            quality = 85
            encoded = b""
            while True:
                buffer = io.BytesIO()
                work.save(buffer, format="JPEG", quality=quality)
                encoded = buffer.getvalue()
                if len(encoded) <= max_bytes or quality <= 40:
                    break
                quality -= 15
            if len(encoded) > max_bytes:
                raise ImageProcessingError(
                    f"Page {page_index}: image is still {len(encoded)} bytes "
                    f"after downscaling (limit {max_bytes})."
                )
            return PreparedImage(
                page_index=page_index,
                media_type=MEDIA_TYPE_JPEG,
                data=encoded,
                width=work.size[0],
                height=work.size[1],
            )
    except ImageProcessingError:
        raise
    except Exception as exc:
        raise ImageProcessingError(
            f"Page {page_index}: image could not be decoded ({exc})."
        ) from exc


def prepare_images(
    images: Iterable[Any],
    *,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[PreparedImage]:
    """Validate and prepare multiple page images, preserving order.
    Page-level: one bad page fails with its page index in the error."""
    return [
        prepare_image(image, page_index=index, max_dimension=max_dimension, max_bytes=max_bytes)
        for index, image in enumerate(images)
    ]
