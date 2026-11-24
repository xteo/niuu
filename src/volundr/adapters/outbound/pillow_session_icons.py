"""Session icon processing with Pillow (and pillow-heif for HEIC).

Decoding is restricted to PNG, JPEG and HEIF. The image must be square and within the
contract's pixel range (:mod:`niuu.domain.session_icon`); EXIF orientation is applied,
then every served size is resampled from the upright source (smaller squares are scaled
up, never stretched) and written as a fresh PNG that carries no metadata: no EXIF, no
text chunks, no ICC profile. Encoding is deterministic, so the same upload always
yields the same ETag.
"""

from __future__ import annotations

import hashlib
import io

import pillow_heif
from PIL import Image, ImageOps

from niuu.domain.session_icon import (
    ICON_INPUT_TYPES_TEXT,
    ICON_MAX_INPUT_BYTES,
    ICON_MAX_INPUT_PX,
    ICON_MIN_INPUT_PX,
    ICON_SIZE,
    ICON_SIZES,
)
from volundr.domain.session_icons import SessionIconError, SessionIconImages, SessionIconProcessor

pillow_heif.register_heif_opener()
Image.init()

#: Pillow format names decoding is restricted to (HEIC decodes as HEIF).
INPUT_FORMATS: tuple[str, ...] = ("PNG", "JPEG", "HEIF")
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in _ALPHA_MODES or (image.mode == "P" and "transparency" in image.info)


class PillowSessionIconProcessor(SessionIconProcessor):
    """See the module docstring."""

    def process(self, data: bytes) -> SessionIconImages:
        if len(data) > ICON_MAX_INPUT_BYTES:
            raise SessionIconError(
                f"icon is {len(data)} bytes; the limit is {ICON_MAX_INPUT_BYTES} bytes (2 MiB). "
                "Send a smaller image.",
                status=413,
            )
        if not data:
            raise SessionIconError(f"icon is empty; send a {ICON_INPUT_TYPES_TEXT} image")
        try:
            with Image.open(io.BytesIO(data), formats=INPUT_FORMATS) as source:
                width, height = source.size
                self._check_shape(width, height)
                upright = ImageOps.exif_transpose(source)
                upright = upright.convert("RGBA" if _has_alpha(upright) else "RGB")
        except SessionIconError:
            raise
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise SessionIconError(
                f"icon must be a {ICON_INPUT_TYPES_TEXT} image; these bytes are not one"
            ) from exc
        renditions = {size: self._encode(upright, size) for size in ICON_SIZES}
        etag = hashlib.sha256(renditions[ICON_SIZE]).hexdigest()
        return SessionIconImages(etag=etag, renditions=renditions)

    @staticmethod
    def _check_shape(width: int, height: int) -> None:
        if width != height:
            raise SessionIconError(
                f"icon must be 1:1 (square); this image is {width}x{height}. "
                "Crop or pad it to a square first."
            )
        if width < ICON_MIN_INPUT_PX or width > ICON_MAX_INPUT_PX:
            raise SessionIconError(
                f"icon must be {ICON_MIN_INPUT_PX} to {ICON_MAX_INPUT_PX} px on a side; "
                f"this image is {width}x{height}. Resize it (512x512 is ideal)."
            )

    @staticmethod
    def _encode(image: Image.Image, size: int) -> bytes:
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        # A fresh image: nothing of the source's info (EXIF, ICC, text) can reach the PNG.
        clean = Image.new(resized.mode, resized.size)
        clean.paste(resized)
        buffer = io.BytesIO()
        clean.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
