"""Tests for sprite_storage extraction logic."""

import io
from unittest.mock import MagicMock, patch

from PIL import Image

from core.services.sprite_storage import (
    DIRECTION_ROWS,
    FRAME_SIZE,
    SHEET_HEIGHT,
    SHEET_WIDTH,
    build_static_spritesheet_from_rotations,
)



def _make_solid_rotation_png(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGBA", (96, 96), color + (255,))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_frame_size_is_96():
    from core.services.sprite_storage import FRAME_SIZE, SHEET_WIDTH, SHEET_HEIGHT
    assert FRAME_SIZE == 96
    assert SHEET_WIDTH == 576  # 6 * 96
    assert SHEET_HEIGHT == 384  # 4 * 96


def test_build_static_spritesheet_is_288_by_192():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west":  _make_solid_rotation_png((0, 255, 0)),
        "east":  _make_solid_rotation_png((0, 0, 255)),
        "north": _make_solid_rotation_png((255, 255, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)

    sheet = Image.open(io.BytesIO(png))
    assert sheet.size == (SHEET_WIDTH, SHEET_HEIGHT)
    assert sheet.size == (576, 384)


def test_build_static_spritesheet_row_columns_are_identical():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west":  _make_solid_rotation_png((0, 255, 0)),
        "east":  _make_solid_rotation_png((0, 0, 255)),
        "north": _make_solid_rotation_png((255, 255, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)
    sheet = Image.open(io.BytesIO(png)).convert("RGBA")

    for col in range(6):
        pixel = sheet.getpixel((col * FRAME_SIZE + FRAME_SIZE // 2, FRAME_SIZE // 2))
        assert pixel[0] > 200 and pixel[1] < 50 and pixel[2] < 50, f"row 0 col {col} not red: {pixel}"

    for col in range(6):
        pixel = sheet.getpixel((col * FRAME_SIZE + FRAME_SIZE // 2, FRAME_SIZE + FRAME_SIZE // 2))
        assert pixel[1] > 200 and pixel[0] < 50 and pixel[2] < 50, f"row 1 col {col} not green: {pixel}"


def test_build_static_spritesheet_missing_direction_is_transparent():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)
    sheet = Image.open(io.BytesIO(png)).convert("RGBA")

    pixel = sheet.getpixel((FRAME_SIZE // 2, FRAME_SIZE + FRAME_SIZE // 2))
    assert pixel[3] == 0, f"row 1 should be transparent: {pixel}"


def test_upload_sprite_to_s3_custom_filename():
    with patch("core.services.sprite_storage.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        from core.services.sprite_storage import upload_sprite_to_s3
        key = upload_sprite_to_s3(b"png-data", "agent-123", "test-bucket", filename="idle.png")
        assert key == "sprites/agent-123/idle.png"
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Key"] == "sprites/agent-123/idle.png"


def test_upload_sprite_to_s3_default_filename():
    with patch("core.services.sprite_storage.boto3") as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3
        from core.services.sprite_storage import upload_sprite_to_s3
        key = upload_sprite_to_s3(b"png-data", "agent-456", "test-bucket")
        assert key == "sprites/agent-456/walk.png"


def _make_test_frame(width: int = 136, height: int = 136, color: tuple = (255, 0, 0, 255)) -> bytes:
    """Create a test PNG frame with a colored rectangle in the center."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(38, 98):
        for y in range(28, 108):
            img.putpixel((x, y), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_composite_animation_spritesheet_4_dirs_6_frames():
    from core.services.sprite_storage import composite_animation_spritesheet, FRAME_SIZE
    frames = _make_test_frame()
    direction_frames = {
        "south": [frames] * 6,
        "west": [frames] * 6,
        "east": [frames] * 6,
        "north": [frames] * 6,
    }
    result = composite_animation_spritesheet(direction_frames)
    assert result is not None
    png_bytes, frame_count = result
    assert frame_count == 6
    sheet = Image.open(io.BytesIO(png_bytes))
    assert sheet.width == 6 * FRAME_SIZE
    assert sheet.height == 4 * FRAME_SIZE


def test_composite_animation_spritesheet_partial_directions():
    from core.services.sprite_storage import composite_animation_spritesheet, FRAME_SIZE
    frames = _make_test_frame()
    direction_frames = {
        "south": [frames] * 4,
        "east": [frames] * 4,
    }
    result = composite_animation_spritesheet(direction_frames)
    assert result is not None
    png_bytes, frame_count = result
    assert frame_count == 4
    sheet = Image.open(io.BytesIO(png_bytes))
    assert sheet.width == 4 * FRAME_SIZE
    assert sheet.height == 4 * FRAME_SIZE


def test_composite_animation_spritesheet_empty_returns_none():
    from core.services.sprite_storage import composite_animation_spritesheet
    assert composite_animation_spritesheet({}) is None
