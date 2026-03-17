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

    # Find walk animation frames in the ZIP
    # Expected structure: animations/walk/<direction>/<frame>.png
    walk_files = [f for f in zf.namelist() if "/walk/" in f.lower() and f.endswith(".png")]
    if not walk_files:
        logger.info("No walk animation frames found in ZIP. Files: %s", zf.namelist()[:20])
        return None

    sheet = Image.new("RGBA", (SHEET_WIDTH, SHEET_HEIGHT), (0, 0, 0, 0))
    directions_found = 0

    for direction_name, row_index in DIRECTION_ROWS.items():
        # Find frames for this direction
        dir_frames = sorted(
            [f for f in walk_files if f"/{direction_name}/" in f],
        )
        if not dir_frames:
            logger.warning("No walk frames for direction %s", direction_name)
            continue

        directions_found += 1
        for frame_idx, frame_path in enumerate(dir_frames[:FRAME_COUNT]):
            try:
                with zf.open(frame_path) as fp:
                    frame_img = Image.open(fp).convert("RGBA")
                    # Resize if needed (should be FRAME_SIZE x FRAME_SIZE)
                    if frame_img.size != (FRAME_SIZE, FRAME_SIZE):
                        frame_img = frame_img.resize((FRAME_SIZE, FRAME_SIZE), Image.NEAREST)
                    sheet.paste(frame_img, (frame_idx * FRAME_SIZE, row_index * FRAME_SIZE))
            except Exception as e:
                logger.warning("Failed to process frame %s: %s", frame_path, e)

    if directions_found == 0:
        logger.info("No matching directions found in walk animation")
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
