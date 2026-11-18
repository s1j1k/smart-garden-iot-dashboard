"""OpenCV growth metrics and plant localisation for a captured plant image.

Classical image processing, tuned by hand; the pre-trained part (finding the
net pots) is in plant_locator.py and reaches here as ``pots``.

* Green coverage / plant area: HSV green threshold over the whole frame (the
  metric logged since the start of the trial, kept so the growth curve stays
  comparable).
* Plant boxes: leaf pixels from the Lab a* (green to magenta) axis, which
  separates leaves from the purple grow light, the lid, the wall and the teal
  net pots better than a fixed hue range. Only the CAMERA_ROI part of the
  frame is searched (default all of it). With the pots known there is one box
  per pot; otherwise leaf blobs are grouped by position into
  CAMERA_PLANT_COUNT plants (area-weighted k-means).
* Reflections: the glossy lid mirrors each plant. The mirror line is found by
  vertical symmetry (mirror_line) and the green below it is removed.
* Real size: leaf area and box size as fractions of the frame change whenever
  the camera moves or zooms, so each frame is also measured against the lid.
  The two net-pot rims are found by colour and shape (find_rims); their
  centres are POT_SPACING_MM apart (lid drawing), which gives millimetres per
  pixel, and each plant gets area_cm2, height_cm and width_cm.
"""

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

from camera_paths import DEBUG_DIR, LAYOUT_JSON

LOWER_GREEN = np.array([25, 40, 40])
UPPER_GREEN = np.array([95, 255, 255])
# 0.02 = 2% green coverage. Prototype until the camera position is fixed.
SPROUT_THRESHOLD = 0.02
MIN_CONTOUR_AREA = 50

GREEN_A_MAX = -6        # Lab a* (OpenCV, 128 = neutral, shifted to 0): leaf is at most this green-ward
MIN_BRIGHTNESS = 40     # Lab L* 0-255; ignore near-black pixels, whose colour is noise
MAX_BRIGHTNESS = 240    # and near-white ones: the backlit wall clips to L* ~248 with a faint green cast
MIN_MIRROR_SPAN = 40    # px: a mirror line is at least this far below the plant's top
MIN_MIRROR_SCORE_POT = 0.2  # edge correlation to trust a mirror line near a pot (else: the pot's base)
MIN_LEAF_FRAC = 0.0002   # leaf blobs under 0.02% of the frame are specks, not leaves
MIN_PLANT_FRAC = 0.0005  # a plant covers at least 0.05% of the frame
STRAY_DISTANCE = 2.0     # a blob this many times further out than its plant's typical leaf...
POT_REACH = 1.5          # leaves more than 1.5 pot widths to the side of every pot are not plants
STRAY_AREA = 0.15        # ...and under this share of the plant's area is not part of it
LEAF_MIN_RADIUS_FRAC = 0.004  # leaf blades are at least ~5 px half-width at 1296 px tall
LEAF_PEAK_MIN = 0.3      # a blade peak under 30 % of the widest one is an edge, not a leaf
LEAF_NECK = 0.6          # two peaks are one leaf unless the blob narrows below 60 % between

BOX_COLOURS = ((0, 200, 255), (255, 120, 0), (200, 0, 255), (0, 255, 120))

# Scale from the lid. The pot holes are 207 mm apart, centre to centre
# (report lid drawing, holes 57.1 mm). The net-pot rim sitting on each hole is
# 70 mm across and 3 mm thick (net-pot drawing, 18 Oct). In frames where both
# rims are whole (5, 13 and 15 Oct) the detected rim spans 0.367 of the pot
# spacing, i.e. it reads 8.6 % wider than drawn: the colour mask's closing and
# the rim's lit outer edge widen it. Pot spacing is the ruler when both rims
# are seen; a single rim is used at its apparent width.
POT_SPACING_MM = 207.0
RIM_DIAMETER_MM = 70.0
RIM_APPARENT = 1.086          # detected rim width / drawn rim diameter
RIM_HSV_LOW = (100, 170, 40)
RIM_HSV_HIGH = (113, 255, 255)
RIM_MIN_FRAC = 0.002          # a rim covers at least 0.2 % of the frame
RIM_ASPECT = (1.8, 8.0)       # seen from the side a round rim is a wide ellipse
RIM_WIDTH_FRAC = (0.08, 0.45)
RIM_AGREE = 0.2               # two whole rims whose widths differ more than 20 %: one is hidden


def plant_count() -> int:
    """How many plants to box (CAMERA_PLANT_COUNT, default 2)."""
    try:
        return max(1, int(os.getenv("CAMERA_PLANT_COUNT", "2")))
    except ValueError:
        return 2


def roi_box(shape) -> tuple[int, int, int, int]:
    """Pixel box (x0, y0, x1, y1) where plants are looked for.

    CAMERA_ROI="left,top,right,bottom" as fractions of the frame, e.g.
    "0,0,1,0.8" to ignore the bottom fifth (plant reflections in the glossy
    lid). Default: the whole frame.
    """
    height, width = shape[:2]
    try:
        left, top, right, bottom = (float(v) for v in os.getenv("CAMERA_ROI", "0,0,1,1").split(","))
    except ValueError:
        left, top, right, bottom = 0.0, 0.0, 1.0, 1.0
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        left, top, right, bottom = 0.0, 0.0, 1.0, 1.0
    return int(left * width), int(top * height), int(right * width), int(bottom * height)


def leaf_mask(bgr: np.ndarray) -> np.ndarray:
    """255 where a pixel looks like leaf.

    Uses the a* axis of CIE Lab (green to magenta): leaves sit on the green side
    whatever the brightness, while the purple grow light, the lid and the wall
    lean magenta and the teal net pots and white stems sit near neutral. The
    excess-green index missed pale leaves under the purple light.
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lightness = lab[..., 0]
    a = lab[..., 1].astype(np.int16) - 128
    b = lab[..., 2].astype(np.int16) - 128
    mask = (a <= GREEN_A_MAX) & (b > a) & (lightness > MIN_BRIGHTNESS) & (lightness < MAX_BRIGHTNESS)
    mask = mask.astype(np.uint8) * 255
    x0, y0, x1, y1 = roi_box(mask.shape)
    mask[:y0, :] = 0
    mask[y1:, :] = 0
    mask[:, :x0] = 0
    mask[:, x1:] = 0
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def _cluster(centres: np.ndarray, weights: np.ndarray, k: int) -> np.ndarray:
    """Area-weighted k-means on leaf centres; returns a cluster index per leaf.

    Starts from k equal-weight bands left to right (the pots sit side by side),
    so the result is the same every run.
    """
    order = np.argsort(centres[:, 0])
    cumulative = np.cumsum(weights[order]) / weights.sum()
    labels = np.empty(len(centres), dtype=int)
    labels[order] = np.minimum((cumulative * k - 1e-9).astype(int), k - 1)
    for _ in range(20):
        if len(np.unique(labels)) < k:
            break  # one leaf outweighs a whole band: keep the left-to-right split
        means = np.array([
            np.average(centres[labels == c], axis=0, weights=weights[labels == c])
            for c in range(k)
        ])
        distances = np.linalg.norm(centres[:, None, :] - means[None, :, :], axis=2)
        new = distances.argmin(axis=1)
        if np.array_equal(new, labels) or len(np.unique(new)) < k:
            break
        labels = new
    return labels


def _drop_strays(members: list[int], stats: np.ndarray, centroids: np.ndarray) -> list[int]:
    """Leave out small blobs far from the rest of their plant (glare, a green
    cable, algae), so one speck does not stretch the box across the frame."""
    areas = stats[members, cv2.CC_STAT_AREA].astype(float)
    centre = np.average(centroids[members], axis=0, weights=areas)
    distance = np.linalg.norm(centroids[members] - centre, axis=1)
    typical = np.average(distance, weights=areas)
    keep = (distance <= STRAY_DISTANCE * max(typical, 1.0)) | (areas >= STRAY_AREA * areas.sum())
    return [m for m, k in zip(members, keep) if k]


def load_pots(path: Path = LAYOUT_JSON) -> list[list[float]]:
    """Net-pot boxes (fractions x0, y0, x1, y1) found by the pre-trained model
    (plant_locator.py via scripts/locate_plants.py).

    [] until it has run, and also unless it found every pot (CAMERA_PLANT_COUNT):
    leaves are only kept near a known pot, so a missed pot would silently drop
    its plant from the measurements. Grouping by position is used instead.
    """
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
        pots = [[float(v) for v in pot["box"]] for pot in layout.get("pots", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return pots if len(pots) == plant_count() else []


# A photo taken under the white flash has a median a* (green-magenta axis, 0 =
# neutral) of about 11-26 over its lit pixels; one taken under the purple grow
# light alone (flash missing: controller offline, or a light fault) about 77.
# Under red and blue LEDs leaves reflect almost no green, so such a photo cannot
# be colour-corrected into a measurable one: it is skipped instead.
MAGENTA_A_MAX = 45


def white_light(bgr: np.ndarray) -> tuple[bool, float]:
    """(whether the photo was lit white, the median a* of its lit pixels)."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    lit = lab[..., 0] > 60
    if not lit.any():
        return False, float("nan")
    a_median = float(np.median(lab[..., 1][lit].astype(np.int16) - 128))
    return a_median <= MAGENTA_A_MAX, a_median


def find_rims(bgr: np.ndarray) -> list[dict]:
    """The net-pot rims in a frame, left to right (at most plant_count()).

    Each: ``box`` (x, y, w, h px) and ``edge`` (True when the rim touches the
    frame border, so its width is cut short).
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, RIM_HSV_LOW, RIM_HSV_HIGH)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    height, width = mask.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    found = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if (area < RIM_MIN_FRAC * width * height
                or not RIM_ASPECT[0] <= w / max(h, 1) <= RIM_ASPECT[1]
                or not RIM_WIDTH_FRAC[0] * width <= w <= RIM_WIDTH_FRAC[1] * width):
            continue
        found.append((area, {"box": [x, y, w, h],
                             "edge": x <= 1 or y <= 1 or x + w >= width - 1 or y + h >= height - 1}))
    found = [rim for _, rim in sorted(found, key=lambda item: -item[0])[:plant_count()]]
    return sorted(found, key=lambda rim: rim["box"][0])


def frame_scale(rims: list[dict]) -> tuple[float | None, str]:
    """(millimetres per pixel, how it was found) from the rims, or (None, reason).

    Two whole rims of similar width: their centre spacing is POT_SPACING_MM.
    Otherwise the widest whole rim is RIM_DIAMETER_MM x RIM_APPARENT across. A rim cut by the
    frame border is not used; a frame with none is left unscaled.
    """
    whole = [rim for rim in rims if not rim["edge"]]
    if len(whole) >= 2:
        (x0, _, w0, _), (x1, _, w1, _) = whole[0]["box"], whole[-1]["box"]
        if abs(w0 - w1) <= RIM_AGREE * max(w0, w1):
            spacing = (x1 + w1 / 2) - (x0 + w0 / 2)
            if spacing > 0:
                return POT_SPACING_MM / spacing, "pot spacing"
    if whole:
        widest = max(rim["box"][2] for rim in whole)
        return RIM_DIAMETER_MM * RIM_APPARENT / widest, "rim width"
    return None, "no whole pot rim in view"


def _add_real_size(plants: list[dict], mm_per_px: float | None, source: str) -> None:
    """area_cm2 / height_cm / width_cm per plant, plus the scale used (None when
    the frame could not be scaled: such frames are left out of growth curves)."""
    for plant in plants:
        plant["mm_per_px"] = mm_per_px
        plant["scale_source"] = source
        if mm_per_px is None:
            continue
        cm = mm_per_px / 10
        _, _, w, h = plant["box"]
        plant["area_cm2"] = round(plant["area"] * cm * cm, 2)
        plant["height_cm"] = round(h * cm, 2)
        plant["width_cm"] = round(w * cm, 2)


def vertical_edges(bgr: np.ndarray) -> np.ndarray:
    """Strength of vertical edges. A reflection in the lid keeps the plant's
    outline even where its colour is too faint for the leaf mask, and vertical
    edges survive an up-down flip while the wall's horizontal lines are ignored."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return cv2.GaussianBlur(np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)), (5, 5), 0)


def mirror_line(edges: np.ndarray, x0: int, x1: int, top: int,
                y_lo: int, y_hi: int) -> tuple[int | None, float]:
    """Row of the horizontal mirror line under a plant, and how well it fits.

    A glossy lid is a flat mirror: below the line where a plant meets the lid
    is a flipped copy of the plant. For each candidate row between ``y_lo`` and
    ``y_hi`` the plant's columns ``x0:x1`` from its ``top`` down to the row are
    flipped and compared (normalised cross-correlation of edge strength) with
    the same height below the row; the best match wins. This is the
    single-camera stand-in for ground-plane rejection in vehicle perception,
    where anything that appears below the road plane, such as a puddle
    reflection, is not a real object.
    """
    height = edges.shape[0]
    best, best_score = None, -1.0
    for y in range(max(y_lo, top + MIN_MIRROR_SPAN), y_hi, 3):
        span = y - top
        if y + span > height:
            break
        above = edges[top:y, x0:x1][::-1].ravel()
        below = edges[y:y + span, x0:x1].ravel()
        above = above - above.mean()
        below = below - below.mean()
        norm = np.sqrt((above * above).sum() * (below * below).sum())
        score = float((above * below).sum() / norm) if norm > 0 else 0.0
        if score > best_score:
            best, best_score = y, score
    return best, max(best_score, 0.0)


def _leaf_count(mask: np.ndarray) -> int:
    """Leaves in one plant's mask, including leaves that touch or overlap.

    Seen from the side, the leaves of a bushy plant (three basil seedlings in one
    pot) merge into one blob, so counting blobs gives 1. Instead: open away the
    stems, then find the broad parts of the blob with a distance transform. Each
    leaf blade is a thickness peak; two peaks are one leaf unless the blob
    narrows between them (a long lettuce leaf has several peaks along it with no
    neck), and peaks under LEAF_PEAK_MIN of the widest leaf are edge noise.
    """
    min_r = max(3, round(LEAF_MIN_RADIUS_FRAC * mask.shape[0]))
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * min_r + 1, 2 * min_r + 1))
    body = cv2.morphologyEx(mask, cv2.MORPH_OPEN, disk)
    dt = cv2.distanceTransform(body, cv2.DIST_L2, 5)
    window = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4 * min_r + 1, 4 * min_r + 1))
    peaks = ((dt >= cv2.dilate(dt, window) - 1e-3) & (dt >= min_r)).astype(np.uint8)
    _, _, _, centres = cv2.connectedComponentsWithStats(peaks)
    found = sorted(((float(dt[int(c[1]), int(c[0])]), c) for c in centres[1:]),
                   key=lambda p: -p[0])
    if not found:
        return 0
    floor = LEAF_PEAK_MIN * found[0][0]
    kept = []
    for r, c in found:  # one peak per blade: drop plateaus next to a bigger peak
        if r >= floor and all(np.hypot(*(c - c2)) > 1.6 * max(r, r2) for r2, c2 in kept):
            kept.append((r, c))
    parent = list(range(len(kept)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i, (ri, ci) in enumerate(kept):
        for j in range(i + 1, len(kept)):
            rj, cj = kept[j]
            steps = max(2, int(np.hypot(*(ci - cj))))
            xs = np.linspace(ci[0], cj[0], steps).astype(int)
            ys = np.linspace(ci[1], cj[1], steps).astype(int)
            if dt[ys, xs].min() >= LEAF_NECK * min(ri, rj):  # no neck: same leaf
                parent[root(j)] = root(i)
    return len({root(i) for i in range(len(kept))})


def _leaves(mask: np.ndarray) -> tuple[int, int]:
    """(leaves, leaf pixels) in one plant's mask, ignoring specks."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    min_leaf = MIN_LEAF_FRAC * mask.shape[0] * mask.shape[1]
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = np.isin(labels, np.flatnonzero(areas >= min_leaf) + 1)
    blobs = int((areas >= min_leaf).sum())
    count = _leaf_count(keep.astype(np.uint8) * 255)
    return max(blobs, count), int(areas[areas >= min_leaf].sum())


def _plant(mask: np.ndarray, number: int, anchor: str, reflection: int) -> dict | None:
    """Measurements of one plant from its own mask (reflection already removed)."""
    height, width = mask.shape
    leaf_count, area = _leaves(mask)
    if area < MIN_PLANT_FRAC * width * height:
        return None
    x, y, w, h = cv2.boundingRect(mask)
    return {
        "number": number,
        "anchor": anchor,
        "box": [int(x), int(y), int(w), int(h)],
        "area": area,
        "area_frac": area / (width * height),
        "height_frac": h / height,
        "width_frac": w / width,
        "leaf_count": leaf_count,
        "reflection_frac": reflection / (width * height),
    }


def _remove_reflection(own: np.ndarray, edges: np.ndarray, y_lo: int, y_hi: int,
                       min_score: float, fallback: int | None) -> tuple[np.ndarray, int, int | None]:
    """Cut ``own`` (one plant's mask) at its mirror line, or at ``fallback`` when
    no line scores ``min_score``. Returns the mask without the reflection, the
    reflection's leaf pixels and the line (None if nothing was cut)."""
    rows = np.flatnonzero(own.any(axis=1))
    cols = np.flatnonzero(own.any(axis=0))
    if len(rows) == 0:
        return own, 0, None
    line, score = mirror_line(edges, int(cols[0]), int(cols[-1]) + 1, int(rows[0]), y_lo, y_hi)
    if line is None or score < min_score:
        line = fallback
    if line is None:
        return own, 0, None
    reflection = int(np.count_nonzero(own[line:]))
    kept = own.copy()
    kept[line:] = 0
    return kept, reflection, line


def _by_pot(mask: np.ndarray, edges: np.ndarray, pots: list[list[float]]) -> list[dict]:
    """One box per net pot (a pot may hold several seedlings).

    Each leaf blob goes to the pot nearest it sideways; blobs more than
    POT_REACH pot widths from every pot are not plants. Each pot's green is then
    cut at its mirror line, or at the pot's base if no mirror image is found.
    """
    height, width = mask.shape
    boxes = np.array(pots) * [width, height, width, height]
    centres = (boxes[:, 0] + boxes[:, 2]) / 2
    widths = np.maximum(boxes[:, 2] - boxes[:, 0], 1)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    owner = np.full(n, -1)
    for blob in range(1, n):
        reach = np.abs(centroids[blob, 0] - centres) / widths
        nearest = int(np.argmin(reach))
        if reach[nearest] <= POT_REACH:
            owner[blob] = nearest
    plants = []
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        members = [blob for blob in range(1, n) if owner[blob] == index]
        members = _drop_strays(members, stats, centroids) if members else []
        own = np.isin(labels, members).astype(np.uint8) * 255
        pot_h = y1 - y0
        # The plant meets the lid near its pot, so the mirror line is searched
        # around the pot; if none stands out, the pot's base is the cut.
        own, reflection, line = _remove_reflection(
            own, edges, int(y0 - 0.3 * pot_h), int(y1 + 0.3 * pot_h),
            MIN_MIRROR_SCORE_POT, int(y1))
        plant = _plant(own, index + 1, "pot", reflection)
        if plant is not None:
            plant["mirror_y"] = line
            plants.append(plant)
    return plants


def find_plants(bgr: np.ndarray, count: int | None = None,
                pots: list[list[float]] | None = None) -> list[dict]:
    """Boxes around the plants, numbered left to right, with reflections removed.

    With ``pots`` (net-pot boxes from the pre-trained model, as fractions of the
    frame) there is one box per pot, numbered by pot, so an empty pot does not
    renumber the others. Without them, leaf blobs (stems are thin and pale, so
    each leaf is its own blob) are grouped into ``count`` plants by position.

    Each plant: ``number``, ``anchor`` ("pot" or "position"), ``box`` (x, y, w,
    h in pixels), ``area`` (leaf pixels), ``area_frac`` (of the frame),
    ``height_frac`` / ``width_frac`` (box size / frame size), ``leaf_count``
    (leaf blades, touching ones split at the narrow joins; still rough when
    leaves hide each other), ``stretch`` / ``fill`` / ``leaf_l`` / ``leaf_a`` /
    ``leaf_b`` (health inputs, see _add_shape_and_colour), ``reflection_frac`` (green removed as
    a reflection) and ``mirror_y`` (the cut line in pixels, or None).
    """
    mask = leaf_mask(bgr)
    edges = vertical_edges(bgr)
    rims = find_rims(bgr)
    if not pots and len(rims) == plant_count():
        # No layout from the pre-trained model yet: the rims found by colour are
        # the pots (better than grouping leaves by position).
        height, width = bgr.shape[:2]
        pots = [[x / width, y / height, (x + w) / width, (y + h) / height]
                for x, y, w, h in (rim["box"] for rim in rims)]
    plants = _by_pot(mask, edges, pots) if pots else _by_position(mask, count)
    _add_shape_and_colour(bgr, mask, plants)
    _add_real_size(plants, *frame_scale(rims))
    return plants


def _add_shape_and_colour(bgr: np.ndarray, mask: np.ndarray, plants: list[dict]) -> None:
    """Health inputs per plant (plant_health.py reads them from plants.jsonl).

    ``stretch``: box height / sqrt(leaf area). A plant that grows in proportion
    keeps it; one that elongates (thin stems, long petioles, leaves held far
    apart) raises it, so its trend shows legginess. ``fill``: leaf area / box
    area. ``leaf_l`` / ``leaf_a`` / ``leaf_b``: mean CIE Lab of the leaf pixels
    above the mirror line (L* 0-100, a* and b* signed): a* rising towards 0 is a
    leaf losing green, b* rising is yellowing. Colour depends on the light, so it
    is only compared between photos taken under the same light.
    """
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    for plant in plants:
        x, y, w, h = plant["box"]
        bottom = y + h if plant.get("mirror_y") is None else min(y + h, plant["mirror_y"])
        region = mask[y:bottom, x:x + w] > 0
        plant["stretch"] = plant["height_frac"] / max(plant["area_frac"], 1e-9) ** 0.5
        plant["fill"] = plant["area"] / max(w * h, 1)
        if region.sum() < 50:
            continue
        pixels = lab[y:bottom, x:x + w][region]
        plant["leaf_l"] = round(float(pixels[:, 0].mean()) * 100 / 255, 1)
        plant["leaf_a"] = round(float(pixels[:, 1].mean()) - 128, 1)
        plant["leaf_b"] = round(float(pixels[:, 2].mean()) - 128, 1)


def _by_position(mask: np.ndarray, count: int | None) -> list[dict]:
    """Plants by grouping leaf blobs by position, when the pots are not known."""
    shape = mask.shape
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    min_leaf = MIN_LEAF_FRAC * shape[0] * shape[1]
    leaves = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_leaf]
    if not leaves:
        return []
    areas = stats[leaves, cv2.CC_STAT_AREA].astype(float)
    k = min(count or plant_count(), len(leaves))
    groups = _cluster(centroids[leaves], areas, k)
    plants = []
    for group in range(k):
        members = [leaves[i] for i in np.flatnonzero(groups == group)]
        members = _drop_strays(members, stats, centroids) if members else []
        if not members:
            continue
        # No reflection cut without pots: with no prior for where the lid is,
        # the best mirror match on a real frame sliced through a lettuce. The
        # leaf mask already leaves out most of a reflection (its colour is faint).
        own = np.isin(labels, members).astype(np.uint8) * 255
        plant = _plant(own, 0, "position", 0)
        if plant is not None:
            plant["mirror_y"] = None
            plants.append(plant)
    plants.sort(key=lambda p: p["box"][0])
    for number, plant in enumerate(plants, start=1):
        plant["number"] = number
    return plants


def draw_plants(bgr: np.ndarray, plants: list[dict]) -> np.ndarray:
    """Draw a numbered box around each plant, in place. Returns the image."""
    scale = max(0.5, bgr.shape[1] / 1600)
    thickness = max(2, int(round(2 * scale)))
    x0, y0, x1, y1 = roi_box(bgr.shape)
    if (x0, y0, x1, y1) != (0, 0, bgr.shape[1], bgr.shape[0]):
        cv2.rectangle(bgr, (x0, y0), (x1 - 1, y1 - 1), (200, 200, 200), 1)   # CAMERA_ROI
    for plant in plants:
        number = plant["number"]
        x, y, w, h = plant["box"]
        colour = BOX_COLOURS[(number - 1) % len(BOX_COLOURS)]
        cv2.rectangle(bgr, (x, y), (x + w, y + h), colour, thickness)
        mirror = plant.get("mirror_y")
        if mirror is not None and plant.get("reflection_frac", 0) > 0:
            # Where the lid's reflection was cut off: dashed, under the plant.
            for dash in range(x, x + w, 12):
                cv2.line(bgr, (dash, mirror), (min(dash + 6, x + w), mirror), colour, 1)
            cv2.putText(bgr, "reflection removed", (x, mirror + int(16 * scale)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45 * scale, colour, 1)
        name = "Pot" if plant.get("anchor") == "pot" else "Plant"
        label = f"{name} {number}  {plant['area_frac'] * 100:.1f}%  {plant['leaf_count']} leaves"
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, thickness)
        top = max(0, y - th - base - 4)
        cv2.rectangle(bgr, (x, top), (x + tw + 6, top + th + base + 4), colour, -1)
        cv2.putText(bgr, label, (x + 3, top + th + 2), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6 * scale, (0, 0, 0), thickness)
    return bgr


def analyse_image(image_path):
    image_path = Path(image_path)
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")

    # Full frame until a fixed plant ROI is calibrated.
    roi = image.copy()
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, LOWER_GREEN, UPPER_GREEN)
    kernel = np.ones((5, 5), np.uint8)
    green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_OPEN, kernel)
    green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_CLOSE, kernel)

    green_pixels = cv2.countNonZero(green_mask)
    total_pixels = green_mask.shape[0] * green_mask.shape[1]
    green_ratio = green_pixels / total_pixels
    green_percentage = green_ratio * 100

    contours, _ = cv2.findContours(
        green_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    valid_contours = []
    plant_area = 0.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area > MIN_CONTOUR_AREA:
            valid_contours.append(contour)
            plant_area += area

    sprouted = green_ratio >= SPROUT_THRESHOLD
    plants = find_plants(roi, pots=load_pots())

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    debug_image = roi.copy()
    cv2.drawContours(debug_image, valid_contours, -1, (0, 255, 0), 2)
    draw_plants(debug_image, plants)
    status_text = "Sprouted" if sprouted else "Not Sprouted"
    cv2.putText(
        debug_image,
        f"Green: {green_percentage:.2f}%",
        (30, 50),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        debug_image,
        f"Plant Area: {plant_area:.0f}px",
        (30, 90),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (255, 255, 255),
        2,
    )
    cv2.putText(
        debug_image,
        status_text,
        (30, 130),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 255, 0) if sprouted else (0, 0, 255),
        2,
    )
    debug_path = DEBUG_DIR / f"debug_{image_path.name}"
    cv2.imwrite(str(debug_path), debug_image)

    return {
        "green_ratio": green_ratio,
        "green_percentage": green_percentage,
        "plant_area": plant_area,
        "sprouted": sprouted,
        "plants": plants,
        "debug_image": str(debug_path),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python growth_detection.py <image_path>")
        sys.exit(1)
    result = analyse_image(sys.argv[1])
    print(f"Green Coverage: {result['green_percentage']:.2f}%")
    print(f"Plant Area: {result['plant_area']:.0f} px")
    print(f"Sprouted: {result['sprouted']}")
    for number, plant in enumerate(result["plants"], start=1):
        print(f"Plant {number}: box {plant['box']}  {plant['area_frac'] * 100:.2f}% of frame")
    print(f"Debug Image: {result['debug_image']}")
