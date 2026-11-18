#!/usr/bin/env python3
"""Draw the app icons in src/pwa_assets/ (a leaf under a blurple grow-light glow).

Run once after changing the design:  python scripts/make_pwa_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "src" / "pwa_assets"
BG = (27, 21, 48)
GLOW = (139, 92, 246)
LEAF = (76, 196, 106)
VEIN = (32, 120, 60)


def draw(size, padding=0.0, rounded=True):
    scale = 4  # draw large, then downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    base = ImageDraw.Draw(img)
    radius = int(s * 0.22) if rounded else 0
    base.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=BG)

    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((s * 0.18, s * 0.02, s * 0.82, s * 0.5), fill=GLOW + (210,))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.08))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    # Light bar
    d.rounded_rectangle((s * 0.26, s * 0.17, s * 0.74, s * 0.23), radius=int(s * 0.03),
                        fill=(225, 210, 255))

    # Leaf: two arcs meeting at the tip, inset by the maskable safe-zone padding.
    inset = s * padding
    box = (inset + s * 0.27, inset * 0.5 + s * 0.33, s - inset - s * 0.27, s - inset - s * 0.1)
    w, h = box[2] - box[0], box[3] - box[1]
    leaf = Image.new("L", (s, s), 0)
    ld = ImageDraw.Draw(leaf)
    ld.ellipse((box[0] - w * 0.35, box[1], box[2] - w * 0.05, box[3] + h * 0.1), fill=255)
    mask2 = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask2).ellipse((box[0] + w * 0.05, box[1], box[2] + w * 0.35, box[3] + h * 0.1),
                                  fill=255)
    leaf = Image.composite(leaf, Image.new("L", (s, s), 0), mask2)
    img.paste(Image.new("RGBA", (s, s), LEAF + (255,)), (0, 0), leaf)
    d = ImageDraw.Draw(img)
    cx = (box[0] + box[2]) / 2
    d.line((cx, box[1] + h * 0.12, cx, box[3] + h * 0.04), fill=VEIN, width=int(s * 0.025))
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192).save(OUT / "icon-192.png")
    draw(512).save(OUT / "icon-512.png")
    draw(512, padding=0.08, rounded=False).save(OUT / "icon-maskable-512.png")
    draw(180, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png")
    # Monochrome badge for the Android status bar.
    badge = draw(96).getchannel("A").point(lambda a: 255 if a > 0 else 0)
    Image.merge("LA", (badge, badge)).save(OUT / "badge.png")
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
