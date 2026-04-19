"""Sprite storage service: download from PixelLab ZIP, composite, upload to S3."""

import io
import logging
from typing import Optional

import boto3
from PIL import Image

from core.config import settings

logger = logging.getLogger(__name__)

# Sprite sheet layout: 6 frames x 96px wide, 4 directions x 96px tall
FRAME_COUNT = 6
FRAME_SIZE = 96
SHEET_WIDTH = FRAME_COUNT * FRAME_SIZE  # 576
SHEET_HEIGHT = 4 * FRAME_SIZE  # 384

# Row index for each direction in the output spritesheet
DIRECTION_ROWS = {"south": 0, "west": 1, "east": 2, "north": 3}


def _crop_and_resize(img: Image.Image, target_size: int) -> Image.Image:
    """Crop transparent padding around the character, then resize to fill the target frame.

    PixelLab returns characters at ~60% of the canvas with transparent padding.
    Simply resizing the full canvas makes the character tiny. Instead:
    1. Find the bounding box of non-transparent pixels
    2. Crop to that bbox with a small margin
    3. Resize to fill the target frame (maintaining aspect ratio, centered)
    """
    bbox = img.getbbox()
    if bbox is None:
        # Fully transparent — return empty frame
        return Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))

    # Add 1px margin around the content
    margin = 1
    x1 = max(0, bbox[0] - margin)
    y1 = max(0, bbox[1] - margin)
    x2 = min(img.width, bbox[2] + margin)
    y2 = min(img.height, bbox[3] + margin)

    cropped = img.crop((x1, y1, x2, y2))

    # Resize to fill the target frame while maintaining aspect ratio
    cw, ch = cropped.size
    scale = min(target_size / cw, target_size / ch)
    new_w = max(1, int(cw * scale))
    new_h = max(1, int(ch * scale))
    resized = cropped.resize((new_w, new_h), Image.NEAREST)

    # Center on a transparent target-sized canvas
    result = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    paste_x = (target_size - new_w) // 2
    paste_y = target_size - new_h  # Anchor to bottom (feet on ground)
    result.paste(resized, (paste_x, paste_y))
    return result


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

    sheet_width = frame_count * FRAME_SIZE
    sheet = Image.new("RGBA", (sheet_width, SHEET_HEIGHT), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        frames = direction_frames.get(direction_name)
        if not frames:
            continue
        directions_found += 1
        for frame_idx, frame_bytes in enumerate(frames[:frame_count]):
            try:
                frame_img = Image.open(io.BytesIO(frame_bytes)).convert("RGBA")
                frame_img = _crop_and_resize(frame_img, FRAME_SIZE)
                sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
            except Exception as e:
                logger.warning("Failed to process V2 frame %s/%d: %s", direction_name, frame_idx, e)

    if directions_found == 0:
        return None

    logger.info(
        "Composited V2 animation spritesheet: %d directions, %d frames",
        directions_found,
        frame_count,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count


def build_static_spritesheet_from_rotations(
    rotation_bytes: dict[str, bytes],
) -> bytes:
    """Build a 576x384 spritesheet from 4 direction rotation PNGs.

    Each rotation PNG is cropped+resized to FRAME_SIZE (96x96) and
    duplicated across all 6 columns of its direction's row. Missing
    directions remain transparent.
    """
    sheet = Image.new("RGBA", (SHEET_WIDTH, SHEET_HEIGHT), (0, 0, 0, 0))

    for direction, row in DIRECTION_ROWS.items():
        png = rotation_bytes.get(direction)
        if not png:
            continue
        try:
            img = Image.open(io.BytesIO(png)).convert("RGBA")
        except Exception as exc:
            logger.warning("Bad rotation PNG for %s: %s", direction, exc)
            continue

        frame = _crop_and_resize(img, FRAME_SIZE)
        for col in range(FRAME_COUNT):
            sheet.paste(frame, (col * FRAME_SIZE, row * FRAME_SIZE), frame)

    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


def composite_animation_spritesheet(
    direction_frames: dict[str, list[bytes]],
    frame_size: int = FRAME_SIZE,
) -> tuple[bytes, int] | None:
    """Composite per-direction frame PNGs into a multi-direction spritesheet.

    Takes pre-grouped {direction: [frame_png_bytes]} and composites into a
    single spritesheet with frame_count columns and 4 direction rows.
    Each frame is cropped and resized to frame_size via _crop_and_resize.

    Returns (spritesheet_png_bytes, frame_count) or None if no valid frames.
    """
    if not direction_frames:
        return None

    frame_count = max(len(frames) for frames in direction_frames.values())
    if frame_count == 0:
        return None

    sheet_width = frame_count * frame_size
    sheet_height = 4 * frame_size
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
                frame_img = _crop_and_resize(frame_img, frame_size)
                sheet.paste(frame_img, (frame_idx * frame_size, row_index * frame_size))
            except Exception as e:
                logger.warning("Failed to process animation frame %s/%d: %s", direction_name, frame_idx, e)

    if directions_found == 0:
        return None

    logger.info(
        "Composited animation spritesheet: %d directions, %d frames, %dpx",
        directions_found,
        frame_count,
        frame_size,
    )
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue(), frame_count


def upload_sprite_to_s3(png_bytes: bytes, agent_id: str, bucket: str | None = None, filename: str = "walk.png") -> str:
    """Upload a sprite PNG to S3 and return the S3 key.

    Key pattern: sprites/{agent_id}/{filename}
    """
    bucket = bucket or settings.SPRITE_S3_BUCKET
    s3 = boto3.client("s3")
    key = f"sprites/{agent_id}/{filename}"
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
