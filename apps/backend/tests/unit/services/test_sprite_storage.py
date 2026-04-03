"""Tests for sprite_storage extraction logic."""

import io
import zipfile

from PIL import Image

from core.services.sprite_storage import (
    DIRECTION_ROWS,
    FRAME_SIZE,
    extract_all_spritesheets,
    extract_animation_spritesheet,
    extract_walk_spritesheet,
)


def _make_test_zip(animations: dict[str, dict[str, int]]) -> bytes:
    """Build a minimal ZIP with colored frames.
    animations: {animation_name: {direction: frame_count}}
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for anim_name, directions in animations.items():
            for direction, frame_count in directions.items():
                for i in range(frame_count):
                    img = Image.new("RGBA", (96, 96), (255, 0, 0, 255))
                    frame_buf = io.BytesIO()
                    img.save(frame_buf, format="PNG")
                    zf.writestr(
                        f"animations/{anim_name}/{direction}/frame_{i:04d}.png",
                        frame_buf.getvalue(),
                    )
    return buf.getvalue()


def _make_rotation_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for direction in DIRECTION_ROWS:
            img = Image.new("RGBA", (96, 96), (0, 255, 0, 255))
            frame_buf = io.BytesIO()
            img.save(frame_buf, format="PNG")
            zf.writestr(f"rotations/{direction}.png", frame_buf.getvalue())
    return buf.getvalue()


class TestExtractAnimationSpritesheet:
    def test_all_four_directions(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6, "west": 6, "east": 6, "north": 6}})
        result = extract_animation_spritesheet(zip_bytes, "walk")
        assert result is not None
        png_bytes, frame_count = result
        assert frame_count == 6
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width == 6 * FRAME_SIZE
        assert img.height == 4 * FRAME_SIZE

    def test_partial_directions(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6, "east": 6}})
        result = extract_animation_spritesheet(zip_bytes, "walk")
        assert result is not None
        _, frame_count = result
        assert frame_count == 6

    def test_animation_not_found(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6}})
        assert extract_animation_spritesheet(zip_bytes, "run") is None

    def test_variable_frame_count(self):
        zip_bytes = _make_test_zip({"breathing-idle": {"south": 4, "west": 4, "east": 4, "north": 4}})
        result = extract_animation_spritesheet(zip_bytes, "breathing-idle")
        assert result is not None
        png_bytes, frame_count = result
        assert frame_count == 4
        img = Image.open(io.BytesIO(png_bytes))
        assert img.width == 4 * FRAME_SIZE

    def test_bad_zip(self):
        assert extract_animation_spritesheet(b"not a zip", "walk") is None


class TestExtractAllSpritesheets:
    def test_multiple_animations(self):
        zip_bytes = _make_test_zip({
            "walk": {"south": 6, "west": 6, "east": 6, "north": 6},
            "breathing-idle": {"south": 4, "west": 4, "east": 4, "north": 4},
        })
        results = extract_all_spritesheets(zip_bytes)
        assert "walk" in results
        assert "breathing-idle" in results
        assert results["walk"][1] == 6
        assert results["breathing-idle"][1] == 4

    def test_empty_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        assert extract_all_spritesheets(buf.getvalue()) == {}

    def test_bad_zip(self):
        assert extract_all_spritesheets(b"not a zip") == {}


class TestExtractWalkSpritesheetBackwardCompat:
    def test_returns_png_bytes_from_walk_animation(self):
        zip_bytes = _make_test_zip({"walk": {"south": 6, "west": 6, "east": 6, "north": 6}})
        result = extract_walk_spritesheet(zip_bytes)
        assert result is not None
        img = Image.open(io.BytesIO(result))
        assert img.width == 6 * FRAME_SIZE

    def test_falls_back_to_rotations(self):
        zip_bytes = _make_rotation_zip()
        result = extract_walk_spritesheet(zip_bytes)
        assert result is not None
        img = Image.open(io.BytesIO(result))
        assert img.width == 6 * FRAME_SIZE
