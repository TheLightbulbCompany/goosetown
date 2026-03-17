#!/usr/bin/env python3
"""Compose individual PixelLab frames into spritesheets for GooseTown.

Layout: 6 columns (frames) x 4 rows (south, west, east, north)
Each frame: 48x48px
Output: 288x192 PNG per character

For missing walk directions, the static rotation image is duplicated 6 times.
"""

import os
import sys
from pathlib import Path
from PIL import Image

FRAME_W = 48
FRAME_H = 48
FRAMES_PER_DIR = 6
DIRECTIONS = ["south", "west", "east", "north"]  # row order

SPRITE_DIR = Path(__file__).parent


def compose_character(name: str) -> Path:
    char_dir = SPRITE_DIR / name
    if not char_dir.exists():
        raise FileNotFoundError(f"Character directory not found: {char_dir}")

    sheet = Image.new("RGBA", (FRAME_W * FRAMES_PER_DIR, FRAME_H * len(DIRECTIONS)), (0, 0, 0, 0))

    for row, direction in enumerate(DIRECTIONS):
        walk_dir = char_dir / "animations" / "walk" / direction
        if walk_dir.exists():
            frames = sorted(walk_dir.glob("frame_*.png"))
            for col, frame_path in enumerate(frames[:FRAMES_PER_DIR]):
                img = Image.open(frame_path).convert("RGBA")
                if img.size != (FRAME_W, FRAME_H):
                    img = img.resize((FRAME_W, FRAME_H), Image.NEAREST)
                sheet.paste(img, (col * FRAME_W, row * FRAME_H))
            # If fewer than 6 frames, repeat last frame
            if len(frames) < FRAMES_PER_DIR and len(frames) > 0:
                last = Image.open(frames[-1]).convert("RGBA")
                if last.size != (FRAME_W, FRAME_H):
                    last = last.resize((FRAME_W, FRAME_H), Image.NEAREST)
                for col in range(len(frames), FRAMES_PER_DIR):
                    sheet.paste(last, (col * FRAME_W, row * FRAME_H))
        else:
            # Use static rotation image for missing direction
            rot_path = char_dir / "rotations" / f"{direction}.png"
            if rot_path.exists():
                static = Image.open(rot_path).convert("RGBA")
                if static.size != (FRAME_W, FRAME_H):
                    static = static.resize((FRAME_W, FRAME_H), Image.NEAREST)
                for col in range(FRAMES_PER_DIR):
                    sheet.paste(static, (col * FRAME_W, row * FRAME_H))
            else:
                print(f"  WARNING: No walk or rotation for {name}/{direction}")

    out_path = SPRITE_DIR / f"{name}-sheet.png"
    sheet.save(out_path)
    print(f"  Created {out_path.name} ({sheet.size[0]}x{sheet.size[1]})")
    return out_path


def main():
    chars = sys.argv[1:] if len(sys.argv) > 1 else []
    if not chars:
        # Auto-detect: any directory with rotations/
        chars = [d.name for d in SPRITE_DIR.iterdir()
                 if d.is_dir() and (d / "rotations").exists()]
        chars.sort()

    if not chars:
        print("No characters found.")
        return

    print(f"Composing spritesheets for: {', '.join(chars)}")
    for name in chars:
        print(f"\n  Processing {name}...")
        try:
            compose_character(name)
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
