"""Sprite storage service: download from PixelLab ZIP, composite, upload to S3."""

import io
import logging
import zipfile
from typing import Optional

import boto3
from PIL import Image

from core.config import settings

logger = logging.getLogger(__name__)

# Sprite sheet layout: 6 frames x 48px wide, 4 directions x 48px tall
FRAME_COUNT = 6
FRAME_SIZE = 48
SHEET_WIDTH = FRAME_COUNT * FRAME_SIZE  # 288
SHEET_HEIGHT = 4 * FRAME_SIZE  # 192

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


def extract_walk_spritesheet(zip_bytes: bytes) -> Optional[bytes]:
    """Extract walk animation frames from a PixelLab character ZIP and composite into a spritesheet.

    The ZIP contains: animations/<animation_name>/<direction>/<frame>.png
    Walk animations have 6 frames per direction.

    Returns PNG bytes of a 288x192 spritesheet (6 frames x 4 directions),
    or None if walk animation frames are not found in the ZIP.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        logger.error("Invalid ZIP file from PixelLab")
        return None

    all_files = zf.namelist()

    # Try walk animation frames first: animations/walk/<direction>/<frame>.png
    walk_files = [f for f in all_files if "/walk/" in f.lower() and f.endswith(".png")]

    # Fallback: static rotation PNGs (rotations/<direction>.png) — use as single-frame sprites
    rotation_files = [f for f in all_files if f.startswith("rotations/") and f.endswith(".png")]

    if not walk_files and not rotation_files:
        logger.info("No walk or rotation frames found in ZIP. Files: %s", all_files[:20])
        return None

    sheet = Image.new("RGBA", (SHEET_WIDTH, SHEET_HEIGHT), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        if walk_files:
            # Multi-frame walk animation
            dir_frames = sorted([f for f in walk_files if f"/{direction_name}/" in f])
            if not dir_frames:
                continue
            directions_found += 1
            for frame_idx, frame_path in enumerate(dir_frames[:FRAME_COUNT]):
                try:
                    with zf.open(frame_path) as fp:
                        frame_img = Image.open(fp).convert("RGBA")
                        frame_img = _crop_and_resize(frame_img, FRAME_SIZE)
                        sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
                except Exception as e:
                    logger.warning("Failed to process frame %s: %s", frame_path, e)
        else:
            # Single-frame static rotation fallback
            rot_path = f"rotations/{direction_name}.png"
            if rot_path not in all_files:
                continue
            directions_found += 1
            try:
                with zf.open(rot_path) as fp:
                    frame_img = Image.open(fp).convert("RGBA")
                    frame_img = _crop_and_resize(frame_img, FRAME_SIZE)
                    # Repeat the single frame across all columns
                    for frame_idx in range(FRAME_COUNT):
                        sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
            except Exception as e:
                logger.warning("Failed to process rotation %s: %s", rot_path, e)

    if directions_found == 0:
        logger.info("No matching directions found in ZIP")
        return None

    logger.info("Composited walk spritesheet: %d directions", directions_found)
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


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
