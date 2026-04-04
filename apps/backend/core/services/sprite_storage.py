"""Sprite storage service: download from PixelLab ZIP, composite, upload to S3."""

import io
import logging
import zipfile
from typing import Optional

import boto3
from PIL import Image

from core.config import settings

logger = logging.getLogger(__name__)

# Default frame size for legacy spritesheets.
# New spritesheets use full-resolution frames from PixelLab and Godot scales them.
FRAME_COUNT = 6
FRAME_SIZE = 48
SHEET_WIDTH = FRAME_COUNT * FRAME_SIZE  # 288
SHEET_HEIGHT = 4 * FRAME_SIZE  # 192

# Row index for each direction in the output spritesheet
DIRECTION_ROWS = {"south": 0, "west": 1, "east": 2, "north": 3}

# Display size in Godot (pixels). Godot scales the full-res sprite to this.
DISPLAY_SIZE = 48


def _prepare_frame(img: Image.Image, target_size: int | None = None) -> Image.Image:
    """Prepare a frame for the spritesheet.

    If target_size is given (legacy mode), crops transparent padding and resizes
    to fit. Otherwise keeps the original resolution — Godot handles scaling.
    """
    if target_size is None:
        # Full-res mode: return as-is (Godot will scale)
        return img

    # Legacy mode: crop + resize to target
    bbox = img.getbbox()
    if bbox is None:
        return Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))

    margin = 1
    x1 = max(0, bbox[0] - margin)
    y1 = max(0, bbox[1] - margin)
    x2 = min(img.width, bbox[2] + margin)
    y2 = min(img.height, bbox[3] + margin)

    cropped = img.crop((x1, y1, x2, y2))

    cw, ch = cropped.size
    scale = min(target_size / cw, target_size / ch)
    new_w = max(1, int(cw * scale))
    new_h = max(1, int(ch * scale))
    resized = cropped.resize((new_w, new_h), Image.NEAREST)

    result = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    paste_x = (target_size - new_w) // 2
    paste_y = target_size - new_h  # Anchor to bottom (feet on ground)
    result.paste(resized, (paste_x, paste_y))
    return result


def _extract_rotation_fallback(zip_bytes: bytes) -> Optional[bytes]:
    """Composite static rotation PNGs into a walk-like spritesheet (single frame repeated).

    Looks for rotations/<direction>.png in the ZIP and repeats the single frame
    across all FRAME_COUNT columns to produce a standard-sized spritesheet.
    Uses the actual image dimensions from PixelLab (no downscale).

    Returns PNG bytes or None if no rotation files are found.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None

    all_files = zf.namelist()
    rotation_files = [f for f in all_files if f.startswith("rotations/") and f.endswith(".png")]
    if not rotation_files:
        return None

    # Detect frame size from the first rotation image
    first_rot = rotation_files[0]
    with zf.open(first_rot) as fp:
        sample = Image.open(fp)
        frame_w, frame_h = sample.size

    sheet = Image.new("RGBA", (FRAME_COUNT * frame_w, 4 * frame_h), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        rot_path = f"rotations/{direction_name}.png"
        if rot_path not in all_files:
            continue
        directions_found += 1
        try:
            with zf.open(rot_path) as fp:
                frame_img = Image.open(fp).convert("RGBA")
                for frame_idx in range(FRAME_COUNT):
                    sheet.paste(frame_img, (frame_idx * frame_w, row_index * frame_h))
        except Exception as e:
            logger.warning("Failed to process rotation %s: %s", rot_path, e)

    if directions_found == 0:
        return None

    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def extract_animation_spritesheet(
    zip_bytes: bytes, animation_name: str
) -> Optional[tuple[bytes, int]]:
    """Extract a named animation from a PixelLab character ZIP and composite into a spritesheet.

    Looks for: animations/<animation_name>/<direction>/<frame>.png
    Detects frame count dynamically (max frames across all directions found).

    Returns (png_bytes, frame_count) or None if animation is not found.
    The spritesheet dimensions are (frame_count * FRAME_SIZE) x (4 * FRAME_SIZE).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return None

    all_files = zf.namelist()
    prefix = f"animations/{animation_name}/"
    anim_files = [f for f in all_files if f.startswith(prefix) and f.endswith(".png")]

    if not anim_files:
        return None

    # Detect frame count: max frames across all directions present
    frames_per_direction: dict[str, list[str]] = {}
    for direction_name in DIRECTION_ROWS:
        dir_frames = sorted([f for f in anim_files if f"/{direction_name}/" in f])
        if dir_frames:
            frames_per_direction[direction_name] = dir_frames

    if not frames_per_direction:
        return None

    # Detect frame size from the first frame image
    first_frame_path = next(iter(next(iter(frames_per_direction.values()))))
    with zf.open(first_frame_path) as fp:
        sample = Image.open(fp)
        frame_w, frame_h = sample.size

    frame_count = max(len(frames) for frames in frames_per_direction.values())
    sheet_width = frame_count * frame_w
    sheet_height = 4 * frame_h
    sheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        dir_frames = frames_per_direction.get(direction_name)
        if not dir_frames:
            continue
        directions_found += 1
        for frame_idx, frame_path in enumerate(dir_frames[:frame_count]):
            try:
                with zf.open(frame_path) as fp:
                    frame_img = Image.open(fp).convert("RGBA")
                    sheet.paste(frame_img, (frame_idx * frame_w, row_index * frame_h))
            except Exception as e:
                logger.warning("Failed to process frame %s: %s", frame_path, e)

    if directions_found == 0:
        return None

    logger.info(
        "Composited %s spritesheet: %d directions, %d frames",
        animation_name,
        directions_found,
        frame_count,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count


def extract_all_spritesheets(zip_bytes: bytes) -> dict[str, tuple[bytes, int]]:
    """Discover all animations in a PixelLab ZIP and extract each as a spritesheet.

    Scans for animations/<name>/ directories and calls extract_animation_spritesheet
    for each animation found.

    Returns dict mapping animation_name -> (png_bytes, frame_count).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return {}

    all_files = zf.namelist()

    # Discover unique animation names from animations/<name>/ structure
    animation_names: set[str] = set()
    for path in all_files:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "animations" and parts[1]:
            animation_names.add(parts[1])

    results: dict[str, tuple[bytes, int]] = {}
    for anim_name in animation_names:
        result = extract_animation_spritesheet(zip_bytes, anim_name)
        if result is not None:
            results[anim_name] = result

    return results


def composite_v2_animation(
    direction_frames: dict[str, list[bytes]],
) -> Optional[tuple[bytes, int]]:
    """Composite animate-with-text-v2 results into a spritesheet.

    Takes a dict of direction -> list of frame PNG bytes.
    Returns (spritesheet_png_bytes, frame_count) or None.
    """
    if not direction_frames:
        return None

    # Determine frame count from the direction with most frames
    frame_count = max(len(frames) for frames in direction_frames.values())
    if frame_count == 0:
        return None

    # Detect frame size from the first available frame
    first_frames = next(iter(direction_frames.values()))
    sample = Image.open(io.BytesIO(first_frames[0]))
    frame_w, frame_h = sample.size

    sheet_width = frame_count * frame_w
    sheet_height = 4 * frame_h
    sheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        frames = direction_frames.get(direction_name)
        if not frames:
            continue
        directions_found += 1
        for frame_idx, frame_bytes in enumerate(frames[:frame_count]):
            try:
                frame_img = Image.open(io.BytesIO(frame_bytes)).convert("RGBA")
                sheet.paste(frame_img, (frame_idx * frame_w, row_index * frame_h))
            except Exception as e:
                logger.warning("Failed to process V2 frame %s/%d: %s", direction_name, frame_idx, e)

    if directions_found == 0:
        return None

    logger.info(
        "Composited V2 animation spritesheet: %d directions, %d frames",
        directions_found, frame_count,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count


def extract_walk_spritesheet(zip_bytes: bytes) -> Optional[bytes]:
    """Extract walk animation spritesheet from a PixelLab character ZIP.

    Backward-compatible wrapper: calls extract_animation_spritesheet for "walk",
    falling back to _extract_rotation_fallback if no walk animation is found.

    Returns PNG bytes of a spritesheet or None if nothing usable is found.
    """
    result = extract_animation_spritesheet(zip_bytes, "walk")
    if result is not None:
        png_bytes, _ = result
        return png_bytes

    fallback = _extract_rotation_fallback(zip_bytes)
    if fallback is not None:
        return fallback

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        all_files = zf.namelist()
    except zipfile.BadZipFile:
        return None
    logger.info("No walk or rotation frames found in ZIP. Files: %s", all_files[:20])
    return None


def upload_sprite_to_s3(png_bytes: bytes, agent_id: str, bucket: str | None = None) -> str:
    """Upload a sprite PNG to S3 and return the S3 key.

    Key pattern: sprites/{agent_id}/walk.png
    """
    bucket = bucket or settings.SPRITE_S3_BUCKET
    s3 = boto3.client("s3")
    key = f"sprites/{agent_id}/walk.png"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=png_bytes,
        ContentType="image/png",
        CacheControl="public, max-age=31536000",
    )
    logger.info("Uploaded sprite to s3://%s/%s", bucket, key)
    return key


def upload_sprites_to_s3(
    animations: dict[str, tuple[bytes, int]],
    agent_id: str,
    bucket: str | None = None,
) -> dict[str, dict]:
    """Upload multiple animation spritesheets to S3.

    Takes a dict of animation_name -> (png_bytes, frame_count) and uploads each
    to sprites/{agent_id}/{animation_name}.png.

    Returns dict mapping animation_name -> {"key": s3_key, "frame_count": N}.
    """
    bucket = bucket or settings.SPRITE_S3_BUCKET
    s3 = boto3.client("s3")
    results: dict[str, dict] = {}

    for anim_name, (png_bytes, frame_count) in animations.items():
        key = f"sprites/{agent_id}/{anim_name}.png"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=png_bytes,
            ContentType="image/png",
            CacheControl="public, max-age=31536000",
        )
        logger.info("Uploaded %s sprite to s3://%s/%s", anim_name, bucket, key)
        results[anim_name] = {"key": key, "frame_count": frame_count}

    return results
