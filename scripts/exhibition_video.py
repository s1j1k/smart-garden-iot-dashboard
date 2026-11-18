#!/usr/bin/env python3
"""Exhibition loop video: the timelapse three times over, each pass with one layer.

  1. What the camera sees      the detector's box around each plant, leaf area growing
  2. What the sensors measured  water, nutrients, pH, air, CO2 and light, cursor at the frame
  3. When will it be ready?     the harvest random forest retrained as each day's
                                growth joins its table: one of its trees, the table,
                                the 200 trees, what it relies on, and the harvest dates

Title cards fade from and to black, so the file loops without a seam.

  .venv/bin/python scripts/exhibition_video.py --out data/camera/exhibition.mp4
  .venv/bin/python scripts/exhibition_video.py --preview /tmp/exhibition   # PNGs only

1920x1080 at 24 fps. Uses the same harvest code and settings as the dashboard
(src/harvest.py, the planting date and breaks in garden_settings).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(os.getenv("SMART_GARDEN_REPO") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "src"))

import development  # noqa: E402
import garden_settings  # noqa: E402
import harvest  # noqa: E402
import plant_growth  # noqa: E402
import timelapse_export  # noqa: E402
from sqlite_store import load_hourly_conditions  # noqa: E402

W, H, FPS = 1920, 1080, 24
CARD_FRAMES = 3 * FPS
HOLD_FRAMES = 3 * FPS   # the finished forest, held at the end of pass 3
FADE = 12
SQLITE = ROOT / "data" / "sensor_readings.sqlite"
PLANTS_JSONL = ROOT / "data" / "camera" / "plants.jsonl"
LOCAL_TZ = "Australia/Melbourne"

# The poster's palette (Canva, A0): white page, near-black text, navy headings,
# rounded panels outlined in blue / green / yellow / red with pastel fills; the
# dashboard's purple for its address.
BG = (255, 255, 255)
PANEL = (255, 255, 255)
GRID = (223, 228, 233)
TEXT = (35, 31, 32)          # poster body text #231f20
MUTED = (93, 102, 112)
NAVY = (1, 75, 121)          # poster headings #014b79
ACCENT = (58, 157, 93)       # poster green
TEAL = (11, 93, 122)
BLUE = NAVY
PURPLE = (124, 92, 255)      # dashboard accent #7c5cff
OUTLINE = {"green": (76, 175, 80), "blue": (58, 143, 212), "yellow": (240, 190, 40),
           "red": (226, 64, 63)}
PASTEL = {"green": (213, 234, 207), "blue": (196, 217, 240), "yellow": (249, 242, 197)}
POT_COLOURS = {1: (58, 157, 93), 2: (47, 134, 201)}
WHITE = (255, 255, 255)
FONT_DIR = Path("/usr/share/fonts/truetype")
# Canva Sans is Canva's own font; Nunito Sans is the nearest one installed on the Pi.
SANS = FONT_DIR / "nunito-sans" / "NunitoSans-VariableFont_YTLC,opsz,wdth,wght.ttf"
MONO = FONT_DIR / "noto" / "NotoSansMono-Regular.ttf"
pass_outline = OUTLINE["green"]  # set by each pass before it draws its panels


def panel(draw, box, fill=PANEL):
    draw.rounded_rectangle(box, radius=18, fill=fill, outline=pass_outline, width=4)


def leaf(draw, x0, y0, length, angle, width, fill):
    """A leaf shape from (x0, y0) along ``angle`` (radians, 0 = right, up is negative y)."""
    ux, uy = math.cos(angle), -math.sin(angle)
    px, py = -uy, ux
    side_a, side_b = [], []
    for k in range(13):
        t = k / 12
        half = width * math.sin(math.pi * t) * (1 - 0.35 * t)
        cx, cy = x0 + ux * length * t, y0 + uy * length * t
        side_a.append((cx + px * half, cy + py * half))
        side_b.append((cx - px * half, cy - py * half))
    draw.polygon(side_a + side_b[::-1], fill=fill)


def seedling(draw, x, y, size):
    """The 🌱 mark of the dashboard and the poster's QR code, drawn (no emoji font on the Pi)."""
    stem = (46, 125, 50)
    draw.line((x, y + size, x, y + size * 0.42), fill=stem, width=max(2, int(size / 9)))
    leaf(draw, x, y + size * 0.48, size * 0.62, math.radians(150), size * 0.2, (102, 187, 106))
    leaf(draw, x, y + size * 0.42, size * 0.72, math.radians(35), size * 0.24, ACCENT)


def growth_text(pct: float) -> tuple:
    """growth_colour darkened enough to read on white."""
    return mix(growth_colour(pct), (0, 0, 0), 0.35)

PHOTO_BIG = (40, 150, 1280, 720)      # x, y, w, h
PHOTO_SMALL = (40, 150, 768, 432)
RIGHT = (1360, 150, 520, 880)


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    if mono:
        return ImageFont.truetype(str(MONO), size)
    f = ImageFont.truetype(str(SANS), size)
    try:
        # Axes: weight, width, optical size (6 = spaced for small text), YTLC.
        f.set_variation_by_axes([700 if bold else 400, 100, 6 if size <= 18 else 12, 500])
    except (OSError, ValueError):
        pass
    return f


def text_w(draw: ImageDraw.ImageDraw, s: str, f) -> int:
    return int(draw.textlength(s, font=f))


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def growth_colour(pct: float) -> tuple:
    """Red to green over -60..+60 %/day, as in the report's tree figure (RdYlGn)."""
    stops = [(-60, (215, 48, 39)), (-30, (252, 141, 89)), (0, (254, 224, 139)),
             (30, (145, 207, 96)), (60, (26, 152, 80))]
    pct = max(-60.0, min(60.0, pct))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if pct <= p1:
            return mix(c0, c1, (pct - p0) / (p1 - p0))
    return stops[-1][1]


def pct_day(rgr: float) -> float:
    return 100 * (math.exp(rgr) - 1)


def signed_pct(value: float) -> str:
    return "±0" if round(value) == 0 else f"{value:+.0f}"


# ------------------------------------------------------------------ data
def photo_time(path: Path) -> datetime:
    return datetime.strptime(path.name[:19], "%Y-%m-%d_%H-%M-%S")


def load_boxes() -> dict[str, list]:
    boxes = {}
    for line in PLANTS_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        boxes[entry.get("image")] = [(p["number"], p["box"]) for p in entry.get("plants", [])
                                     if p.get("box")]
    return boxes


def load_growth() -> dict[int, pd.DataFrame]:
    """Each plant's measurements (area_cm2, height_cm, width_cm, leaf_count): hourly
    medians, then a trailing 12 h rolling median so single bad frames do not spike."""
    plants = plant_growth.load_plants()
    out = {}
    for number, one in plants.groupby("plant"):
        one = one.set_index(pd.to_datetime(one["timestamp"]))
        cols = [c for c in ("area_cm2", "height_cm", "width_cm", "leaf_count") if c in one]
        hourly = one[cols].apply(pd.to_numeric, errors="coerce").resample("1h").median()
        out[int(number)] = hourly.rolling(12, min_periods=3).median()
    return out


SENSORS = [  # column, label, unit, decimals
    ("water_temp_c", "Water temperature", "°C", 1),
    ("ph", "pH", "", 2),
    ("tds_ppm", "Nutrients (TDS)", "ppm", 0),
    ("water_level_pct", "Water level", "%", 0),
    ("ambient_temp_c", "Air temperature", "°C", 1),
    ("ambient_humidity_pct", "Humidity", "%", 0),
    ("co2_ppm", "CO₂", "ppm", 0),
    ("led_on", "Grow light", "", 0),
]


def load_sensors(start: datetime, end: datetime) -> pd.DataFrame:
    cols = ", ".join(c for c, *_ in SENSORS)
    since = (pd.Timestamp(start).tz_localize(LOCAL_TZ).tz_convert("UTC")
             - pd.Timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    df = pd.read_sql_query(f"select timestamp, {cols} from sensor_readings where timestamp >= ?",
                           con, params=(since,))
    con.close()
    when = pd.to_datetime(df.pop("timestamp"), utc=True, errors="coerce")
    df.index = when.dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
    df = df[df.index.notna()].apply(pd.to_numeric, errors="coerce").sort_index()
    df = df.resample("10min").mean()
    return df[(df.index >= start - timedelta(hours=1)) & (df.index <= end + timedelta(hours=1))]


def forest_snapshots(end: datetime) -> list[dict]:
    """The harvest forest as it stood each time a day's growth row became known.

    A row for day d needs day d+1's leaf area, so it counts from 21:00 on d+1."""
    from sklearn.ensemble import RandomForestRegressor

    settings = garden_settings.load()
    pots = [str(p).lower() for p in settings.get("pots") or settings.get("plants") or []]
    planted = garden_settings.planted_at(settings)
    since = planted.astimezone(timezone.utc).isoformat()
    hourly = development.hourly_conditions(load_hourly_conditions(str(SQLITE), since))
    breaks = development.parse_breaks(settings.get("breaks") or [])
    start = planted.astimezone().replace(tzinfo=None) if planted.tzinfo else planted
    conds = {c: development.daily_development(hourly, c, start, end, breaks,
                                              settings.get("grow_light_ppfd"))
             for c in set(pots)}
    sizes = harvest.daily_sizes(plant_growth.load_plants())
    table = harvest.training_table(sizes, conds, pots).sort_values(["day", "plant"])
    table["available"] = table["day"] + pd.Timedelta(days=1, hours=21)
    snaps, history = [], {}
    for at in sorted(table["available"].unique()):
        at = pd.Timestamp(at)
        rows = table[table["available"] <= at]
        snap = {"at": at.to_pydatetime(), "rows": rows, "ready": len(rows) >= harvest.MIN_DAYS,
                "new": set(map(tuple, table[table["available"] == at][["day", "plant"]].values))}
        if snap["ready"]:
            fill = rows[harvest.FEATURES].median()
            model = RandomForestRegressor(n_estimators=harvest.TREES, min_samples_leaf=2,
                                          random_state=0, n_jobs=1)
            model.fit(rows[harvest.FEATURES].fillna(fill), rows["rgr"])
            cut = at.normalize()
            result = harvest.forecast(sizes[sizes["day"] <= cut],
                                      {c: d[d.index <= cut] for c, d in conds.items()},
                                      pots, at.to_pydatetime())
            # The fullest of the first 25 trees that still fits the panel (≤ 6 leaves).
            leaves = [e.get_n_leaves() if e.get_n_leaves() <= 6 else 0
                      for e in model.estimators_[:25]]
            shown = int(np.argmax(leaves))
            snap.update(model=model, shown=shown, importance=result.get("importance", {}),
                        pots=result.get("pots", {}))
            for number, pot in snap["pots"].items():
                history.setdefault(number, []).append((at.to_pydatetime(), pot.get("date")))
            snap["history"] = {k: list(v) for k, v in history.items()}
        snaps.append(snap)
    return snaps, pots


# ------------------------------------------------------------------ drawing helpers
PASS_TAGS = {1: "Plant Vision", 2: "Sensing", 3: "Random Forest"}
PASS_OUTLINES = {1: OUTLINE["green"], 2: OUTLINE["blue"], 3: OUTLINE["yellow"]}


def header(draw, number: int, title: str, subtitle: str):
    """Poster-style heading: 🌱 Smart Garden, the pass title in navy, a pill tag."""
    global pass_outline
    pass_outline = PASS_OUTLINES[number]
    seedling(draw, 54, 26, 34)
    draw.text((80, 26), "Smart Garden", font=font(28, bold=True), fill=TEXT)
    draw.text((40, 66), f"{number} · {title}", font=font(46, bold=True), fill=NAVY)
    tag = PASS_TAGS[number]
    f = font(20, bold=True)
    tw = text_w(draw, tag, f)
    draw.rounded_rectangle((W - 40 - tw - 32, 30, W - 40, 62), radius=16, fill=PASTEL["blue"])
    draw.text((W - 40 - tw - 16, 33), tag, font=f, fill=NAVY)
    f = font(22)
    draw.text((W - 40 - text_w(draw, subtitle, f), 88), subtitle, font=f, fill=MUTED)
    for i in range(3):
        c = PASS_OUTLINES[i + 1] if i == number - 1 else GRID
        x = W - 40 - tw - 32 - 24 - (3 - i) * 42
        draw.rounded_rectangle((x, 42, x + 32, 50), radius=4, fill=c)


_last_photo: dict[tuple, Image.Image] = {}


def paste_photo(frame: Image.Image, path: Path, rect, planted: datetime) -> float:
    x, y, w, h = rect
    try:
        im = Image.open(path)
        im.draft("RGB", (w, h))
        im = im.convert("RGB").resize((w, h), Image.BILINEAR)
        _last_photo[(w, h)] = im
    except (OSError, ValueError):
        # A truncated or corrupt photo (one on 16 Oct): show the previous frame again.
        im = _last_photo.get((w, h)) or Image.new("RGB", (w, h), PANEL)
    frame.paste(im, (x, y))
    when = photo_time(path)
    draw = ImageDraw.Draw(frame)
    day = (when.date() - planted.date()).days
    label = f"{when.day} {when:%b %Y  %H:%M}   ·   day {day} since planting"
    f = font(26 if w > 1000 else 18)
    tw = text_w(draw, label, f)
    draw.rounded_rectangle((x + (w - tw) / 2 - 14, y + h - 52, x + (w + tw) / 2 + 14, y + h - 14),
                           radius=8, fill=(0, 0, 0))
    draw.text((x + (w - tw) / 2, y + h - 48 + (0 if w > 1000 else 6)), label, font=f, fill=WHITE)
    return w / 2304


class Axis:
    """Maps time and value to pixels inside a chart rectangle."""

    def __init__(self, rect, t0: datetime, t1: datetime, lo: float, hi: float):
        self.x, self.y, self.w, self.h = rect
        self.t0, self.span = t0, (t1 - t0).total_seconds()
        self.lo, self.hi = lo, hi if hi > lo else lo + 1

    def px(self, t: datetime) -> float:
        return self.x + self.w * (t - self.t0).total_seconds() / self.span

    def py(self, v: float) -> float:
        return self.y + self.h - self.h * (v - self.lo) / (self.hi - self.lo)


def polyline(series: pd.Series, axis: Axis, until: datetime | None = None) -> list[list]:
    """Pixel runs of a series (broken at gaps), optionally only up to ``until``."""
    s = series if until is None else series[series.index <= until]
    runs, run, last = [], [], None
    for t, v in s.items():
        if pd.isna(v):
            if len(run) > 1:
                runs.append(run)
            run = []
            continue
        if last is not None and (t - last) > pd.Timedelta(hours=2) and len(run) > 1:
            runs.append(run)
            run = []
        run.append((axis.px(t.to_pydatetime()), axis.py(float(v))))
        last = t
    if len(run) > 1:
        runs.append(run)
    return runs


def value_at(series: pd.Series, t: datetime, max_age=pd.Timedelta(minutes=40)):
    s = series.dropna()
    i = s.index.searchsorted(pd.Timestamp(t), side="right") - 1
    if i < 0 or pd.Timestamp(t) - s.index[i] > max_age:
        return None
    return float(s.iloc[i])


def card(frame_i: int, title: str, subtitle: str, accent=NAVY, sub_colour=MUTED):
    """Title card on the poster's white page; fades from and to white, so the loop
    joins on white."""
    im = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(im)
    brand = "Smart Garden"
    fb = font(34, bold=True)
    bw = text_w(draw, brand, fb) + 52
    seedling(draw, (W - bw) / 2 + 18, 368, 44)
    draw.text(((W - bw) / 2 + 52, 372), brand, font=fb, fill=TEXT)
    for s, f, c, y in ((title, font(88, bold=True), accent, 450),
                       (subtitle, font(34), sub_colour, 590)):
        draw.text(((W - text_w(draw, s, f)) / 2, y), s, font=f, fill=c)
    draw.rounded_rectangle((W / 2 - 120, 680, W / 2 + 120, 688), radius=4, fill=OUTLINE["green"])
    fade = min(1.0, frame_i / FADE, (CARD_FRAMES - 1 - frame_i) / FADE)
    if fade < 1:
        im = Image.blend(Image.new("RGB", (W, H), BG), im, max(0.0, fade))
    return im


# ------------------------------------------------------------------ pass 1
class CameraPass:
    def __init__(self, frames, boxes, growth, pots, planted):
        self.frames, self.boxes, self.growth, self.pots, self.planted = frames, boxes, growth, pots, planted
        t0, t1 = photo_time(frames[0]), photo_time(frames[-1])
        # Scale to the typical size, not the odd day-long jump; higher values ride the top edge.
        top = 1.6 * max((g["area_cm2"].quantile(0.9) for g in growth.values()
                         if "area_cm2" in g and g["area_cm2"].notna().any()), default=10)
        for g in growth.values():
            if "area_cm2" in g:
                g["area_plot"] = g["area_cm2"].clip(upper=top * 1.08)
        self.axes, self.static = {}, Image.new("RGB", (RIGHT[2], RIGHT[3]), BG)
        d = ImageDraw.Draw(self.static)
        for k, number in enumerate(sorted(growth)[:2]):
            y0 = k * 330
            d.rounded_rectangle((0, y0, RIGHT[2], y0 + 310), radius=18, fill=PANEL, outline=OUTLINE["green"], width=4)
            rect = (RIGHT[0] + 24, RIGHT[1] + y0 + 150, RIGHT[2] - 48, 130)
            ax = Axis(rect, t0, t1, 0, top * 1.1)
            self.axes[number] = ax
            local = (24, y0 + 150, RIGHT[2] - 48, 130)
            d.line((local[0], local[1] + local[3], local[0] + local[2], local[1] + local[3]), fill=GRID, width=2)
            for run in polyline(growth[number]["area_plot"], ax):
                d.line([(px - RIGHT[0], py - RIGHT[1]) for px, py in run], fill=GRID, width=3)
        d.text((24, 690), "How it works", font=font(22, bold=True), fill=TEXT)
        for i, line in enumerate((
                "A pre-trained detector finds each pot and plant",
                "in every photo. The leaf pixels inside each box",
                "are measured, and the spacing between the pots",
                "turns pixels into centimetres.")):
            d.text((24, 726 + i * 30), line, font=font(20), fill=MUTED)

    def crop(self, number):
        index = number - 1
        return self.pots[index].title() if 0 <= index < len(self.pots) else f"Plant {number}"

    def render(self, i: int) -> Image.Image:
        path = self.frames[i]
        t = photo_time(path)
        frame = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(frame)
        header(draw, 1, "What the camera sees", "each plant found and measured in every photo")
        scale = paste_photo(frame, path, PHOTO_BIG, self.planted)
        x0, y0 = PHOTO_BIG[:2]
        for number, (bx, by, bw, bh) in self.boxes.get(path.name, []):
            c = POT_COLOURS.get(number, ACCENT)
            r = (x0 + bx * scale, y0 + by * scale, x0 + (bx + bw) * scale, y0 + (by + bh) * scale)
            draw.rectangle(r, outline=c, width=4)
            tag = f" {number} · {self.crop(number)} "
            f = font(22, bold=True)
            draw.rectangle((r[0], r[1] - 32, r[0] + text_w(draw, tag, f), r[1]), fill=c)
            draw.text((r[0], r[1] - 30), tag, font=f, fill=BG)
        frame.paste(self.static, RIGHT[:2])
        for k, number in enumerate(sorted(self.growth)[:2]):
            g, ax, top = self.growth[number], self.axes[number], RIGHT[1] + k * 330
            c = POT_COLOURS.get(number, ACCENT)
            draw.text((RIGHT[0] + 24, top + 18), f"Pot {number} · {self.crop(number)}",
                      font=font(24, bold=True), fill=c)
            area = value_at(g["area_cm2"], t, pd.Timedelta(hours=3)) if "area_cm2" in g else None
            draw.text((RIGHT[0] + 24, top + 52),
                      "—" if area is None else f"{area:.1f} cm²", font=font(46, bold=True), fill=TEXT)
            draw.text((RIGHT[0] + 300, top + 70), "leaf area", font=font(20), fill=MUTED)
            bits = []
            for col, fmt in (("height_cm", "height {:.1f} cm"), ("width_cm", "width {:.1f} cm"),
                             ("leaf_count", "{:.0f} leaves")):
                v = value_at(g[col], t, pd.Timedelta(hours=3)) if col in g else None
                if v is not None:
                    bits.append(fmt.format(v))
            draw.text((RIGHT[0] + 24, top + 112), "  ·  ".join(bits), font=font(20), fill=MUTED)
            for run in polyline(g["area_plot"], ax, until=pd.Timestamp(t)):
                draw.line(run, fill=c, width=4)
            cx = ax.px(t)
            draw.line((cx, ax.y, cx, ax.y + ax.h), fill=MUTED, width=1)
        draw.text((PHOTO_BIG[0], PHOTO_BIG[1] + PHOTO_BIG[3] + 30),
                  "Boxes: where the detector found each plant. Leaf area is seen side-on from one camera,",
                  font=font(22), fill=MUTED)
        draw.text((PHOTO_BIG[0], PHOTO_BIG[1] + PHOTO_BIG[3] + 64),
                  "so it wobbles as the leaves move; the curves are a 12-hour rolling median.",
                  font=font(22), fill=MUTED)
        return frame


# ------------------------------------------------------------------ pass 2
class SensorPass:
    def __init__(self, frames, sensors, planted):
        self.frames, self.sensors, self.planted = frames, sensors, planted
        t0, t1 = photo_time(frames[0]), photo_time(frames[-1])
        self.static = Image.new("RGB", (RIGHT[2], RIGHT[3]), BG)
        d = ImageDraw.Draw(self.static)
        self.rows = []
        row_h = RIGHT[3] // len(SENSORS)
        for k, (col, label, unit, dec) in enumerate(SENSORS):
            y = k * row_h
            d.rounded_rectangle((0, y, RIGHT[2], y + row_h - 8), radius=14, fill=PANEL, outline=OUTLINE["blue"], width=3)
            d.text((16, y + 10), label, font=font(20, bold=True), fill=TEXT)
            s = sensors[col] if col in sensors else pd.Series(dtype=float)
            if col == "led_on":
                lo, hi = 0.0, 1.0
            elif s.notna().any():
                lo, hi = float(s.quantile(0.02)), float(s.quantile(0.98))
                pad = (hi - lo) * 0.15 or 1
                lo, hi = lo - pad, hi + pad
            else:
                lo, hi = 0.0, 1.0
            rect = (RIGHT[0] + 16, RIGHT[1] + y + 44, RIGHT[2] - 32, row_h - 64)
            ax = Axis(rect, t0, t1, lo, hi)
            for run in polyline(s, ax):
                d.line([(px - RIGHT[0], py - RIGHT[1]) for px, py in run], fill=GRID, width=2)
            self.rows.append((col, label, unit, dec, s, ax, y))

    def render(self, i: int) -> Image.Image:
        path = self.frames[i]
        t = photo_time(path)
        frame = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(frame)
        header(draw, 2, "What the sensors measured", "a reading every 10 seconds, here as 10-minute means")
        paste_photo(frame, path, PHOTO_BIG, self.planted)
        frame.paste(self.static, RIGHT[:2])
        for col, label, unit, dec, s, ax, y in self.rows:
            v = value_at(s, t)
            if col == "led_on":
                text = "—" if v is None else ("on" if v >= 0.5 else "off")
            else:
                text = "—" if v is None else f"{v:.{dec}f} {unit}".strip()
            f = font(26, bold=True)
            draw.text((RIGHT[0] + RIGHT[2] - 16 - text_w(draw, text, f), RIGHT[1] + y + 6), text,
                      font=f, fill=NAVY if v is not None else MUTED)
            for run in polyline(s, ax, until=pd.Timestamp(t)):
                draw.line(run, fill=BLUE, width=3)
            cx = ax.px(t)
            draw.line((cx, ax.y - 4, cx, ax.y + ax.h + 4), fill=TEXT, width=1)
        draw.text((PHOTO_BIG[0], PHOTO_BIG[1] + PHOTO_BIG[3] + 30),
                  "The controller reads these to dose water, nutrients and pH correctors, and to run "
                  "the grow light.", font=font(22), fill=MUTED)
        draw.text((PHOTO_BIG[0], PHOTO_BIG[1] + PHOTO_BIG[3] + 64),
                  "Gaps: days on the windowsill or with the sensors off.", font=font(22), fill=MUTED)
        return frame


# ------------------------------------------------------------------ pass 3
SHORT = {"log_area": "Plant size", "is_lettuce": "Crop", "f_temp": "Temperature fit",
         "temp_c": "Temperature", "light_hours": "Light hours", "tds_in_band": "Nutrients in range",
         "ph_in_band": "pH in range"}


def describe_split(feature: str, threshold: float) -> str:
    if feature == "log_area":
        return f"Plant size ≤ {math.exp(threshold):.1f} cm²"
    if feature == "is_lettuce":
        return "Crop: basil"
    if feature in ("tds_in_band", "ph_in_band"):
        return f"{SHORT[feature]} ≤ {100 * threshold:.0f} % of day"
    if feature == "temp_c":
        return f"Temperature ≤ {threshold:.1f} °C"
    if feature == "light_hours":
        return f"Light ≤ {threshold:.1f} h"
    if feature == "f_temp":
        return f"Temp. fit ≤ {threshold:.2f}"
    return f"{feature} ≤ {threshold:.2f}"


def tree_layout(tree, node=0, depth=0, x=None):
    x = [0] if x is None else x
    left, right = tree.children_left[node], tree.children_right[node]
    if left == -1:
        pos = {node: (x[0], depth)}
        x[0] += 1
        return pos
    pos = tree_layout(tree, left, depth + 1, x)
    pos.update(tree_layout(tree, right, depth + 1, x))
    pos[node] = ((pos[left][0] + pos[right][0]) / 2, depth)
    return pos


TREE_RECT = (850, 150, 1030, 470)


def draw_tree(estimator, index: int, total: int, max_depth: int) -> Image.Image:
    """One tree of the forest, drawn down to ``max_depth`` (for the build-out)."""
    w, h = TREE_RECT[2], TREE_RECT[3]
    im = Image.new("RGB", (w, h), PANEL)
    d = ImageDraw.Draw(im)
    tree = estimator.tree_
    pos = tree_layout(tree)
    depth = max(p[1] for p in pos.values())
    width = max(p[0] for p in pos.values()) + 1
    d.text((20, 14), f"Tree {index + 1} of {total}  ·  depth {estimator.get_depth()}, "
                     f"{estimator.get_n_leaves()} leaves", font=font(20, bold=True), fill=TEXT)
    xs = lambda px: 90 + (w - 180) * (px + 0.5) / width  # noqa: E731
    ys = lambda py: 110 + (h - 190) * (py / max(depth, 1))  # noqa: E731
    root_n = float(tree.weighted_n_node_samples[0])
    for node, (px, py) in pos.items():
        if py >= max_depth:
            continue
        for child, label in ((tree.children_left[node], "yes"), (tree.children_right[node], "no")):
            if child == -1:
                continue
            cx, cy = pos[child]
            wd = max(2, int(2 + 10 * tree.weighted_n_node_samples[child] / root_n))
            d.line((xs(px), ys(py), xs(cx), ys(cy)), fill=MUTED, width=wd)
            mx, my = (xs(px) + xs(cx)) / 2, (ys(py) + ys(cy)) / 2
            d.text((mx + (-40 if label == "yes" else 12), my - 10), label, font=font(16), fill=MUTED)
    for node, (px, py) in pos.items():
        if py > max_depth:
            continue
        n = int(tree.weighted_n_node_samples[node])
        value = pct_day(float(tree.value[node].ravel()[0]))
        if tree.children_left[node] == -1:
            lines = [f"{signed_pct(value)} %/day", f"{n} rows"]
            fill, outline, colour = growth_colour(value), mix(growth_colour(value), (0, 0, 0), 0.3), TEXT
        else:
            lines = [describe_split(harvest.FEATURES[tree.feature[node]], tree.threshold[node]),
                     f"{n} rows · mean {signed_pct(value)} %/day"]
            fill, outline, colour = BG, BLUE, TEXT
        f = font(17 if width <= 4 else 15)
        bw = max(text_w(d, s, f) for s in lines) + 24
        cx, cy = xs(px), ys(py)
        d.rounded_rectangle((cx - bw / 2, cy - 26, cx + bw / 2, cy + 26), radius=8, fill=fill,
                            outline=outline, width=3)
        for k, s in enumerate(lines):
            d.text((cx - text_w(d, s, f) / 2, cy - 22 + k * 22), s, font=f, fill=colour)
    return im


FOREST_RECT = (850, 640, 1030, 70)


def draw_forest(model) -> Image.Image:
    w, h = FOREST_RECT[2], FOREST_RECT[3]
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    per_row, rows = 100, 2
    step = w / per_row
    for k, e in enumerate(model.estimators_[:per_row * rows]):
        cx = step * (k % per_row) + step / 2
        base = (k // per_row + 1) * (h / rows) - 4
        depth = max(1, e.get_depth())
        top = base - 6 - 7 * depth
        d.line((cx, base, cx, top), fill=MUTED, width=1)
        leaves = e.get_n_leaves()
        for j in range(min(leaves, 5)):
            ang = math.pi * (0.15 + 0.7 * j / max(1, min(leaves, 5) - 1)) if leaves > 1 else math.pi / 2
            lx, ly = cx - math.cos(ang) * 4, top - math.sin(ang) * 4
            d.ellipse((lx - 1.6, ly - 1.6, lx + 1.6, ly + 1.6), fill=ACCENT)
    return im


class ForestPass:
    def __init__(self, frames, snaps, pots, planted):
        self.frames, self.snaps, self.pots, self.planted = frames, snaps, pots, planted
        self.times = [s["at"] for s in snaps]
        self.tree_images, self.forest_images = {}, {}
        for k, s in enumerate(snaps):
            if s["ready"]:
                e = s["model"].estimators_[s["shown"]]
                for depth in range(e.get_depth() + 1):
                    self.tree_images[(k, depth)] = draw_tree(e, s["shown"], len(s["model"].estimators_), depth)
                self.forest_images[k] = draw_forest(s["model"])
        # Frame index at which each snapshot takes over (first photo at or after it).
        ptimes = [photo_time(p) for p in frames]
        self.switch = [bisect_right(ptimes, t - timedelta(seconds=1)) for t in self.times]
        self.t_lo = photo_time(frames[0]).date()
        dates = [p.get(k) for s in snaps if s["ready"] for p in s["pots"].values()
                 for k in ("date", "earliest", "latest")]
        dates = [d for d in dates if d is not None]
        self.d0 = datetime.combine(self.t_lo, datetime.min.time())
        self.d1 = max(dates + [self.d0 + timedelta(days=45)]) + timedelta(days=5)

    def current(self, i: int):
        k = bisect_right(self.switch, i) - 1
        return k, (i - self.switch[k] if k >= 0 else 0)

    def crop(self, number):
        index = number - 1
        return self.pots[index].title() if 0 <= index < len(self.pots) else f"Plant {number}"

    def render(self, i: int, hold: int = 0) -> Image.Image:
        """Frame ``i``; ``hold`` > 0 repeats the last photo with the tree finished."""
        path = self.frames[i]
        frame = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(frame)
        header(draw, 3, "When will it be ready?",
               "a random forest retrained as each day's growth is measured")
        paste_photo(frame, path, PHOTO_SMALL, self.planted)
        k, since = self.current(i)
        since += hold
        snap = self.snaps[k] if k >= 0 else None
        self.table(frame, draw, snap, since)
        if snap is None or not snap["ready"]:
            have = 0 if snap is None else len(snap["rows"])
            panel(draw, (TREE_RECT[0], TREE_RECT[1], TREE_RECT[0] + TREE_RECT[2],
                         TREE_RECT[1] + TREE_RECT[3]))
            msg = f"Collecting data: {have} of {harvest.MIN_DAYS} growth measurements"
            f = font(30, bold=True)
            draw.text((TREE_RECT[0] + (TREE_RECT[2] - text_w(draw, msg, f)) / 2, TREE_RECT[1] + 190),
                      msg, font=f, fill=TEXT)
            sub = "A row needs the plant measured on a day and the day after."
            f2 = font(22)
            draw.text((TREE_RECT[0] + (TREE_RECT[2] - text_w(draw, sub, f2)) / 2, TREE_RECT[1] + 240),
                      sub, font=f2, fill=MUTED)
            return frame
        e = snap["model"].estimators_[snap["shown"]]
        depth = min(e.get_depth(), since // 8)
        frame.paste(self.tree_images[(k, depth)], TREE_RECT[:2])
        draw.rounded_rectangle((TREE_RECT[0], TREE_RECT[1], TREE_RECT[0] + TREE_RECT[2],
                                TREE_RECT[1] + TREE_RECT[3]), radius=18, outline=pass_outline, width=4)
        frame.paste(self.forest_images[k], FOREST_RECT[:2])
        draw.text((FOREST_RECT[0], FOREST_RECT[1] + FOREST_RECT[3] + 4),
                  f"…and the whole forest: {len(snap['model'].estimators_)} trees, each grown on its own "
                  f"resample of the {len(snap['rows'])} rows; the forecast is their median.",
                  font=font(17), fill=MUTED)
        self.importance(draw, k, since)
        self.harvest(draw, snap)
        return frame

    def table(self, frame, draw, snap, since):
        x, y, w = 40, 610, 768
        panel(draw, (x, y, x + w, 1040))
        n = 0 if snap is None else len(snap["rows"])
        draw.text((x + 18, y + 14), f"Training table  ·  {n} rows", font=font(22, bold=True), fill=TEXT)
        draw.text((x + 18, y + 44), "one plant, one day: the conditions, then how much it grew by the next",
                  font=font(16), fill=MUTED)
        cols = [("Day", 70), ("Crop", 90), ("Size", 80), ("Temp", 80), ("Light", 80), ("Nutr.", 80),
                ("pH ok", 80), ("Growth", 110)]
        f = font(17, mono=True)
        cx = x + 18
        for name, cw in cols:
            draw.text((cx, y + 78), name, font=font(17, bold=True), fill=MUTED)
            cx += cw
        if snap is None:
            return
        rows = snap["rows"].tail(11)
        for r, (_, row) in enumerate(rows.iterrows()):
            ry = y + 108 + r * 28
            fresh = (row["day"], row["plant"]) in snap["new"]
            if fresh:
                glow = max(0.0, 1 - since / 48)
                draw.rectangle((x + 8, ry - 2, x + w - 8, ry + 24), fill=mix(PANEL, PASTEL["green"], 0.45 + 0.55 * glow))
            g = pct_day(float(row["rgr"]))
            cells = [f"{row['day']:%d %b}", row["crop"], f"{math.exp(row['log_area']):.1f}",
                     f"{row['temp_c']:.1f}°", f"{row['light_hours']:.1f}h",
                     f"{100 * row['tds_in_band']:.0f}%", f"{100 * row['ph_in_band']:.0f}%", f"{g:+.0f}%/d"]
            cx = x + 18
            for (name, cw), cell in zip(cols, cells):
                colour = growth_text(g) if name == "Growth" else (TEXT if fresh else MUTED)
                draw.text((cx, ry), str(cell), font=f, fill=colour)
                cx += cw

    def importance(self, draw, k, since):
        x, y, w, h = 850, 750, 500, 290
        panel(draw, (x, y, x + w, y + h))
        draw.text((x + 18, y + 14), "What the forest relies on", font=font(22, bold=True), fill=TEXT)
        cur = self.snaps[k]["importance"]
        prev = next((self.snaps[j]["importance"] for j in range(k - 1, -1, -1) if self.snaps[j]["ready"]), cur)
        t = min(1.0, since / 24)
        vals = {f: lerp(prev.get(f, 0), cur.get(f, 0), t) for f in harvest.FEATURES}
        for r, (feat, v) in enumerate(sorted(vals.items(), key=lambda kv: -kv[1])):
            ry = y + 54 + r * 32
            draw.text((x + 18, ry), SHORT[feat], font=font(18), fill=MUTED)
            bx = x + 210
            draw.rounded_rectangle((bx, ry + 4, bx + max(4, int(220 * v)), ry + 20), radius=4, fill=TEAL)
            draw.text((bx + max(4, int(220 * v)) + 8, ry), f"{100 * v:.0f}%", font=font(18), fill=TEXT)

    def harvest(self, draw, snap):
        x, y, w, h = 1380, 750, 500, 290
        panel(draw, (x, y, x + w, y + h))
        draw.text((x + 18, y + 14), "Predicted harvest", font=font(22, bold=True), fill=TEXT)
        span = (self.d1 - self.d0).total_seconds()
        px = lambda d: x + 24 + (w - 48) * (d - self.d0).total_seconds() / span  # noqa: E731
        for r, (number, pot) in enumerate(sorted(snap["pots"].items())):
            ry = y + 56 + r * 108
            c = POT_COLOURS.get(number, ACCENT)
            draw.text((x + 18, ry), f"Pot {number} · {self.crop(number)}", font=font(20, bold=True), fill=c)
            date, lo, hi = pot.get("date"), pot.get("earliest"), pot.get("latest")
            if date is None:
                draw.text((x + 18, ry + 30), "not on track yet (median growth ≤ 0)", font=font(20), fill=MUTED)
            else:
                rng = "" if lo is None or hi is None else f"   range {lo:%d %b} – {hi:%d %b}"
                draw.text((x + 18, ry + 28), f"≈ {date:%d %b}", font=font(30, bold=True), fill=TEXT)
                draw.text((x + 160, ry + 38), rng, font=font(18), fill=MUTED)
            ay = ry + 84
            draw.line((px(self.d0), ay, px(self.d1), ay), fill=GRID, width=2)
            if r == len(snap["pots"]) - 1:
                for d, anchor in ((self.d0, "l"), (self.d1, "r")):
                    s = f"{d:%d %b}"
                    tx = px(d) if anchor == "l" else px(d) - text_w(draw, s, font(14))
                    draw.text((tx, ay - 24), s, font=font(14), fill=MUTED)
            for when, d in snap["history"].get(number, [])[:-1]:
                if d is not None:
                    draw.line((px(d), ay - 6, px(d), ay + 6), fill=MUTED, width=1)
            if date is not None:
                if lo is not None and hi is not None:
                    draw.rounded_rectangle((px(lo), ay - 7, px(hi), ay + 7), radius=6, fill=mix(PANEL, c, 0.45))
                draw.ellipse((px(date) - 7, ay - 7, px(date) + 7, ay + 7), fill=c)
        draw.text((x + 18, y + h - 22), "Harvest sizes are estimates until the first harvest.",
                  font=font(14), fill=MUTED)


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "data" / "camera" / "exhibition.mp4"))
    ap.add_argument("--preview", help="write a few PNGs of each part to this folder instead")
    args = ap.parse_args()

    frames = timelapse_export.frames()
    if not frames:
        print("no timelapse photos")
        return 1
    planted = garden_settings.planted_at()
    planted = planted.astimezone().replace(tzinfo=None) if planted.tzinfo else planted
    t0, t1 = photo_time(frames[0]), photo_time(frames[-1])
    print(f"{len(frames)} photos {t0:%d %b %H:%M} – {t1:%d %b %H:%M}", flush=True)
    snaps, pots = forest_snapshots(datetime.now())
    print(f"forest snapshots: {[(s['at'].strftime('%d %b'), len(s['rows'])) for s in snaps]}", flush=True)
    camera = CameraPass(frames, load_boxes(), load_growth(), pots, planted)
    sensors = SensorPass(frames, load_sensors(t0, t1), planted)
    forest = ForestPass(frames, snaps, pots, planted)
    n = len(frames)
    parts = [
        ("card", ("What the camera sees", f"{n} photos over {(t1 - t0).days} days: each plant found and measured")),
        ("pass", camera),
        ("card", ("What the sensors measured", "water, nutrients, pH, air, CO₂ and light")),
        ("pass", sensors),
        ("card", ("When will it be ready?", "a random forest learns how fast these plants grow here")),
        ("pass", forest),
        ("card", ("Watch it live", "smartgarden.tech", NAVY, PURPLE)),
    ]

    if args.preview:
        out = Path(args.preview)
        out.mkdir(parents=True, exist_ok=True)
        picks = [0, n // 3, (2 * n) // 3, n - 1]
        for p, (kind, what) in enumerate(parts):
            if kind == "card":
                card(CARD_FRAMES // 2, *what).save(out / f"{p}_card.png")
            else:
                for i in picks:
                    what.render(i).save(out / f"{p}_frame{i:04d}.png")
        forest.render(n - 1, hold=HOLD_FRAMES).save(out / "5_hold.png")
        print(f"previews in {out}")
        return 0

    total = len([1 for k, _ in parts if k == "card"]) * CARD_FRAMES + 3 * n + HOLD_FRAMES
    tmp = Path(args.out).with_suffix(".part.mp4")
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE)
    done = 0
    for kind, what in parts:
        if kind == "card":
            jobs = [lambda i=i, what=what: card(i, *what) for i in range(CARD_FRAMES)]
        else:
            jobs = [lambda i=i, what=what: what.render(i) for i in range(n)]
            if what is forest:
                jobs += [lambda h=h: forest.render(n - 1, hold=h) for h in range(1, HOLD_FRAMES + 1)]
        for job in jobs:
            im = job()
            enc.stdin.write(im.tobytes())
            done += 1
            if done % 240 == 0:
                print(f"{done}/{total} frames", flush=True)
    enc.stdin.close()
    if enc.wait() != 0:
        print("ffmpeg failed")
        return 1
    tmp.replace(args.out)
    print(f"wrote {args.out} ({total / FPS:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
