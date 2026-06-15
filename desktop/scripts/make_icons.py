"""Generate a placeholder icon set for the Tauri bundle.

Renders an 'R' on a warm-amber square at 1024x1024, then resizes/exports to
all the formats Tauri references in `tauri.conf.json`:

  icons/32x32.png
  icons/128x128.png
  icons/128x128@2x.png      (256x256)
  icons/icon.png            (used by system tray)
  icons/icon.icns           (macOS app icon)
  icons/icon.ico            (Windows installer + EXE icon)

Replace with a real designed icon before public release.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
SIZES = [16, 32, 64, 128, 256, 512, 1024]

BG = (217, 119, 6, 255)
FG = (26, 15, 0, 255)


def render_base(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, int(size * 0.62))
            break
    if font is None:
        font = ImageFont.load_default()
    text = "R"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), text, fill=FG, font=font)
    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in OUT_DIR.glob("_tmp_*.png"):
        f.unlink()

    print(f"Rendering icon variants to {OUT_DIR}/")
    base = render_base(1024)

    base.resize((32, 32), Image.LANCZOS).save(OUT_DIR / "32x32.png", "PNG")
    base.resize((128, 128), Image.LANCZOS).save(OUT_DIR / "128x128.png", "PNG")
    base.resize((256, 256), Image.LANCZOS).save(OUT_DIR / "128x128@2x.png", "PNG")
    base.resize((64, 64), Image.LANCZOS).save(OUT_DIR / "icon.png", "PNG")

    base.save(
        OUT_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        spec = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]
        for size, name in spec:
            base.resize((size, size), Image.LANCZOS).save(iconset / name, "PNG")
        result = subprocess.run(
            ["iconutil", "-c", "icns", "-o", str(OUT_DIR / "icon.icns"), str(iconset)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"iconutil failed: {result.stderr}")

    produced = sorted(p.name for p in OUT_DIR.iterdir())
    print("Done. Files:")
    for name in produced:
        print(f"  {name}")


if __name__ == "__main__":
    main()
