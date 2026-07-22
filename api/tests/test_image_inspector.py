"""Tests for content-based image inspection and immutable keys."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PIL import Image

from image_inspector import ImageInspectionError, ImageInspectionLimits, ImageInspector
from storage.keys import cover_object_key, page_object_key


def _write_image(path: Path, image_format: str, size: tuple[int, int] = (12, 8)) -> None:
    Image.new("RGB", size, (24, 48, 96)).save(path, format=image_format)


@pytest.fixture()
def inspector(tmp_path: Path) -> ImageInspector:
    return ImageInspector(tmp_path)


@pytest.mark.parametrize(
    ("filename", "image_format", "mime_type", "extension"),
    [
        ("page.jpg", "JPEG", "image/jpeg", "jpg"),
        ("page.png", "PNG", "image/png", "png"),
        ("page.webp", "WEBP", "image/webp", "webp"),
    ],
)
def test_valid_images_are_decoded_and_hashed(
    inspector: ImageInspector,
    tmp_path: Path,
    filename: str,
    image_format: str,
    mime_type: str,
    extension: str,
):
    path = tmp_path / filename
    _write_image(path, image_format)

    result = inspector.inspect(path, expected_width=12, expected_height=8)
    assert len(result.sha256) == 64
    assert result.byte_size == path.stat().st_size
    assert result.width == 12
    assert result.height == 8
    assert result.mime_type == mime_type
    assert result.verified_extension == extension


def test_corrupt_image_is_rejected(inspector: ImageInspector, tmp_path: Path):
    path = tmp_path / "corrupt.jpg"
    path.write_bytes(b"not a JPEG")
    with pytest.raises(ImageInspectionError, match="corrupt|unreadable"):
        inspector.inspect(path)


def test_fake_jpeg_extension_is_rejected(inspector: ImageInspector, tmp_path: Path):
    path = tmp_path / "fake.jpg"
    _write_image(path, "PNG")
    with pytest.raises(ImageInspectionError, match="extension"):
        inspector.inspect(path)


def test_oversized_image_is_rejected(tmp_path: Path):
    path = tmp_path / "large.jpg"
    _write_image(path, "JPEG", size=(64, 64))
    inspector = ImageInspector(tmp_path, ImageInspectionLimits(max_bytes=10, max_pixels=100_000))
    with pytest.raises(ImageInspectionError, match="byte size"):
        inspector.inspect(path)


def test_decompression_bomb_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "bomb.png"
    _write_image(path, "PNG", size=(20, 20))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    inspector = ImageInspector(tmp_path, ImageInspectionLimits(max_bytes=1024 * 1024, max_pixels=10_000))
    with pytest.raises(ImageInspectionError, match="decompression bomb"):
        inspector.inspect(path)


def test_symlink_escape_is_rejected(inspector: ImageInspector, tmp_path: Path):
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}.jpg"
    _write_image(outside, "JPEG")
    link = tmp_path / "escape.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ImageInspectionError, match="outside"):
        inspector.inspect(link)


def test_path_traversal_is_rejected(inspector: ImageInspector, tmp_path: Path):
    path = tmp_path / ".." / tmp_path.name / "missing.jpg"
    with pytest.raises(ImageInspectionError, match="traversal"):
        inspector.inspect(path)


def test_duplicate_bytes_have_same_hash_and_immutable_key(inspector: ImageInspector, tmp_path: Path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    _write_image(first, "JPEG")
    second.write_bytes(first.read_bytes())
    first_result = inspector.inspect(first)
    second_result = inspector.inspect(second)
    series_id = uuid.uuid4()
    chapter_id = uuid.uuid4()
    first_key = page_object_key(series_id, chapter_id, 1, first_result.sha256, first_result.verified_extension)
    second_key = page_object_key(series_id, chapter_id, 1, second_result.sha256, second_result.verified_extension)
    assert first_result.sha256 == second_result.sha256
    assert first_key == second_key


def test_changed_content_same_filename_gets_new_key(inspector: ImageInspector, tmp_path: Path):
    path = tmp_path / "page.jpg"
    series_id = uuid.uuid4()
    chapter_id = uuid.uuid4()
    _write_image(path, "JPEG", size=(12, 8))
    first = inspector.inspect(path)
    first_key = page_object_key(series_id, chapter_id, 1, first.sha256, first.verified_extension)
    _write_image(path, "JPEG", size=(13, 8))
    second = inspector.inspect(path)
    second_key = page_object_key(series_id, chapter_id, 1, second.sha256, second.verified_extension)
    assert first.sha256 != second.sha256
    assert first_key != second_key


def test_cover_key_is_content_addressed():
    series_id = uuid.uuid4()
    digest = "a" * 64
    assert cover_object_key(series_id, digest, "jpg") == (
        f"series/{series_id}/cover-{digest}.jpg"
    )
