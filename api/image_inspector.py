"""Secure, content-based inspection for imported images."""

from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError, DecompressionBombWarning


class ImageInspectionError(ValueError):
    """Raised when an import candidate is unsafe or is not a supported image."""


@dataclass(frozen=True)
class ImageInspectionLimits:
    max_bytes: int = 25 * 1024 * 1024
    max_pixels: int = 100_000_000

    def validate(self) -> None:
        if self.max_bytes < 1 or self.max_pixels < 1:
            raise ValueError("image inspection limits must be positive")


@dataclass(frozen=True)
class ImageInspection:
    source_path: Path
    sha256: str
    byte_size: int
    width: int
    height: int
    format: str
    mime_type: str
    verified_extension: str


class ImageInspector:
    """Inspect images beneath one approved import root."""

    _FORMATS: dict[str, tuple[str, str, frozenset[str]]] = {
        "JPEG": ("image/jpeg", "jpg", frozenset({"jpg", "jpeg"})),
        "PNG": ("image/png", "png", frozenset({"png"})),
        "WEBP": ("image/webp", "webp", frozenset({"webp"})),
    }
    _CANDIDATE_EXTENSIONS = frozenset(
        {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "svg", "avif", "ico"}
    )

    def __init__(
        self,
        import_root: str | Path,
        limits: ImageInspectionLimits | None = None,
        *,
        allow_animated: bool = False,
        supported_formats: frozenset[str] | None = None,
    ) -> None:
        root = Path(import_root).expanduser().resolve()
        if not root.is_dir():
            raise ImageInspectionError("approved import root is not a directory")
        self.import_root = root
        self.limits = limits or ImageInspectionLimits()
        self.limits.validate()
        self.allow_animated = allow_animated
        self.supported_formats = supported_formats or frozenset(self._FORMATS)
        unsupported = self.supported_formats.difference(self._FORMATS)
        if unsupported:
            raise ValueError(f"unsupported image formats configured: {sorted(unsupported)}")

    def _resolve_under_root(self, source_path: str | Path) -> Path:
        candidate = Path(source_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.import_root / candidate
        if ".." in candidate.parts:
            raise ImageInspectionError("path traversal is not allowed")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ImageInspectionError("image file does not exist") from exc
        try:
            resolved.relative_to(self.import_root)
        except ValueError as exc:
            raise ImageInspectionError("image resolves outside the approved import root") from exc
        if not resolved.is_file():
            raise ImageInspectionError("image path is not a regular file")
        return resolved

    def _hash_stream(self, source_path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with source_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    byte_size += len(chunk)
                    if byte_size > self.limits.max_bytes:
                        raise ImageInspectionError("image exceeds the maximum byte size")
                    digest.update(chunk)
        except OSError as exc:
            raise ImageInspectionError("image could not be read") from exc
        return digest.hexdigest(), byte_size

    def inspect(
        self,
        source_path: str | Path,
        *,
        expected_width: int | None = None,
        expected_height: int | None = None,
    ) -> ImageInspection:
        resolved = self._resolve_under_root(source_path)
        sha256, byte_size = self._hash_stream(resolved)
        suffix = resolved.suffix.casefold().lstrip(".")
        if suffix in {"svg", "exe", "com", "bat", "cmd", "sh", "dll"}:
            raise ImageInspectionError("SVG and executable formats are not supported")
        if suffix not in self._CANDIDATE_EXTENSIONS:
            raise ImageInspectionError("file extension is not an image candidate")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DecompressionBombWarning)
                with Image.open(resolved) as image:
                    image_format = (image.format or "").upper()
                    if image_format not in self.supported_formats:
                        raise ImageInspectionError(
                            f"decoded image format is unsupported: {image_format or 'unknown'}"
                        )
                    width, height = image.size
                    if width < 1 or height < 1:
                        raise ImageInspectionError("image dimensions are invalid")
                    if width * height > self.limits.max_pixels:
                        raise ImageInspectionError("image exceeds the maximum pixel count")
                    if expected_width is not None and width != expected_width:
                        raise ImageInspectionError("decoded width does not match expected width")
                    if expected_height is not None and height != expected_height:
                        raise ImageInspectionError("decoded height does not match expected height")
                    if not self.allow_animated and (
                        getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1
                    ):
                        raise ImageInspectionError("animated images are not supported")
                    image.verify()

                # verify() checks the encoded stream; load() decodes the pixels.
                with Image.open(resolved) as decoded:
                    decoded.load()
        except (DecompressionBombError, DecompressionBombWarning) as exc:
            raise ImageInspectionError("image is a decompression bomb") from exc
        except (OSError, UnidentifiedImageError) as exc:
            raise ImageInspectionError("image content is corrupt or unreadable") from exc

        mime_type, verified_extension, allowed_extensions = self._FORMATS[image_format]
        if suffix not in allowed_extensions:
            raise ImageInspectionError("file extension does not match decoded image content")
        return ImageInspection(
            source_path=resolved,
            sha256=sha256,
            byte_size=byte_size,
            width=width,
            height=height,
            format=image_format,
            mime_type=mime_type,
            verified_extension=verified_extension,
        )
