"""Tests for sprite_storage extraction logic."""

import io
from unittest.mock import MagicMock, patch

from PIL import Image

from core.services.sprite_storage import (
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
    from core.services.sprite_storage import FRAME_SIZE, SHEET_HEIGHT, SHEET_WIDTH

    assert FRAME_SIZE == 96
    assert SHEET_WIDTH == 576  # 6 * 96
    assert SHEET_HEIGHT == 384  # 4 * 96


def test_build_static_spritesheet_is_288_by_192():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west": _make_solid_rotation_png((0, 255, 0)),
        "east": _make_solid_rotation_png((0, 0, 255)),
        "north": _make_solid_rotation_png((255, 255, 0)),
    }

    png = build_static_spritesheet_from_rotations(rotations)

    sheet = Image.open(io.BytesIO(png))
    assert sheet.size == (SHEET_WIDTH, SHEET_HEIGHT)
    assert sheet.size == (576, 384)


def test_build_static_spritesheet_row_columns_are_identical():
    rotations = {
        "south": _make_solid_rotation_png((255, 0, 0)),
        "west": _make_solid_rotation_png((0, 255, 0)),
        "east": _make_solid_rotation_png((0, 0, 255)),
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
    from core.services.sprite_storage import FRAME_SIZE, composite_animation_spritesheet

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
    from core.services.sprite_storage import FRAME_SIZE, composite_animation_spritesheet

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


# ---------------------------------------------------------------------------
# Gray-background stripping (the "talking.png renders as a gray box" bug).
#
# animate-with-text frames come back with an opaque (128,128,128) fill behind
# the character. The custom "talking" animation routed through
# composite_animation_spritesheet, which used to skip _strip_gray_background,
# so the whole frame was an opaque gray box in the viewer. The strip now lives
# in the shared _crop_and_resize, so every composite path keys the gray out.
# ---------------------------------------------------------------------------

GRAY = (128, 128, 128, 255)


def _make_gray_bg_frame(width: int = 136, height: int = 136, color: tuple = (200, 30, 30, 255)) -> bytes:
    """A frame with PixelLab's opaque (128,128,128) background + a center character."""
    img = Image.new("RGBA", (width, height), GRAY)
    for x in range(50, 86):
        for y in range(40, 100):
            img.putpixel((x, y), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_strip_gray_background_keys_out_128_corner():
    from core.services.sprite_storage import _strip_gray_background

    img = Image.open(io.BytesIO(_make_gray_bg_frame())).convert("RGBA")
    stripped = _strip_gray_background(img)
    w, h = stripped.size
    # Every corner that was gray is now transparent.
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        assert stripped.getpixel(corner)[3] == 0
    # The character pixels survive.
    assert stripped.getpixel((60, 60)) == (200, 30, 30, 255)


def test_crop_and_resize_finds_character_not_the_gray_canvas():
    from core.services.sprite_storage import FRAME_SIZE, _crop_and_resize

    img = Image.open(io.BytesIO(_make_gray_bg_frame())).convert("RGBA")
    out = _crop_and_resize(img, FRAME_SIZE)
    # Corners transparent (gray stripped before bbox), so the character was
    # cropped to its own bbox and scaled up to fill — not left as a gray box.
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((FRAME_SIZE - 1, FRAME_SIZE - 1))[3] == 0
    # The character fills the frame: the center is opaque and non-gray.
    cx = out.getpixel((FRAME_SIZE // 2, FRAME_SIZE // 2))
    assert cx[3] == 255
    assert cx[:3] != (128, 128, 128)


def test_composite_animation_spritesheet_strips_gray_box():
    """Regression for talking.png: a gray-backed custom animation must composite
    to a sheet with transparent background, not an opaque gray box."""
    from core.services.sprite_storage import composite_animation_spritesheet

    frame = _make_gray_bg_frame()
    direction_frames = {d: [frame] * 4 for d in ("south", "west", "east", "north")}
    result = composite_animation_spritesheet(direction_frames)
    assert result is not None
    png_bytes, _ = result
    sheet = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = sheet.size
    # Sheet corners transparent — pre-fix these were the opaque (128,128,128).
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        assert sheet.getpixel(corner)[3] == 0
    # No opaque PixelLab-gray survives anywhere in the sheet.
    colors = sheet.getcolors(maxcolors=1 << 24)
    assert colors is not None
    assert all(pixel != GRAY for _count, pixel in colors)
