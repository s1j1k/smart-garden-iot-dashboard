"""Plant boxes: leaves grouped into plants, rockwool and stray specks left out."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

cv2 = pytest.importorskip("cv2")  # from apt on the Pi; optional on a laptop
np = pytest.importorskip("numpy")

import growth_detection  # noqa: E402

LEAF = (60, 170, 70)        # BGR
ROCKWOOL = (95, 105, 100)   # grey-green, should not count as leaf


def _scene(tint=(0, 0, 0)):
    image = np.full((540, 960, 3), (70, 70, 75), np.uint8)
    cv2.rectangle(image, (100, 350), (860, 540), ROCKWOOL, -1)
    # Plant 1: three leaves around x=250, separated (pale stems do not show).
    for centre in ((220, 300), (280, 280), (250, 230)):
        cv2.ellipse(image, centre, (30, 15), 20, 0, 360, LEAF, -1)
    # Plant 2: two bigger leaves around x=680.
    for centre in ((630, 260), (740, 250)):
        cv2.ellipse(image, centre, (45, 22), -15, 0, 360, LEAF, -1)
    cv2.circle(image, (930, 40), 6, LEAF, -1)   # a stray green speck
    return cv2.add(image, np.full_like(image, tint))


@pytest.mark.parametrize("tint", [(0, 0, 0), (60, 0, 50)])   # white flash, purple light
def test_two_plants_boxed_left_to_right(tint):
    plants = growth_detection.find_plants(_scene(tint), count=2)
    assert len(plants) == 2
    (x1, y1, w1, h1), (x2, y2, w2, h2) = (p["box"] for p in plants)
    assert 170 <= x1 and x1 + w1 <= 320 and 200 <= y1 and y1 + h1 <= 330
    assert 570 <= x2 and x2 + w2 <= 800
    assert all(p["box"][0] + p["box"][2] < 900 for p in plants)   # speck not included
    assert plants[1]["area"] > plants[0]["area"]


def test_teal_pots_are_not_leaves():
    image = _scene()
    for x in (250, 680):
        cv2.ellipse(image, (x, 340), (90, 25), 0, 0, 360, (150, 120, 20), -1)   # teal BGR
    plants = growth_detection.find_plants(image, count=2)
    assert all(p["box"][1] + p["box"][3] < 320 for p in plants)


def test_roi_drops_reflections(monkeypatch):
    image = _scene()
    for centre in ((250, 480), (680, 470)):                  # reflections below the lid line
        cv2.ellipse(image, centre, (40, 20), 0, 0, 360, LEAF, -1)
    tall = growth_detection.find_plants(image, count=2)
    assert max(p["box"][1] + p["box"][3] for p in tall) > 450
    monkeypatch.setenv("CAMERA_ROI", "0,0,1,0.75")
    cropped = growth_detection.find_plants(image, count=2)
    assert len(cropped) == 2 and all(p["box"][1] + p["box"][3] <= 405 for p in cropped)


def test_leaf_count():
    plants = growth_detection.find_plants(_scene(), count=2)
    assert [p["leaf_count"] for p in plants] == [3, 2]


def test_no_leaves_no_boxes():
    image = np.full((540, 960, 3), ROCKWOOL, np.uint8)
    assert growth_detection.find_plants(image) == []


def test_boxes_are_drawn():
    image = _scene()
    before = image.copy()
    growth_detection.draw_plants(image, growth_detection.find_plants(image, count=2))
    assert not np.array_equal(image, before)


def test_pots_anchor_plants_and_drop_reflections():
    image = _scene()
    for centre in ((250, 480), (680, 470)):                  # reflections below the pots
        cv2.ellipse(image, centre, (40, 20), 0, 0, 360, LEAF, -1)
    cv2.ellipse(image, (60, 120), (25, 12), 0, 0, 360, LEAF, -1)   # green far from any pot
    w, h = 960, 540
    pots = [[190 / w, 310 / h, 310 / w, 360 / h],      # plant 1
            [400 / w, 310 / h, 520 / w, 360 / h],      # empty pot
            [580 / w, 300 / h, 800 / w, 350 / h]]      # plant 2
    plants = growth_detection.find_plants(image, pots=pots)
    assert [p["number"] for p in plants] == [1, 3]     # the empty pot keeps its number
    assert all(p["box"][1] + p["box"][3] <= 360 for p in plants)   # no reflections
    assert plants[0]["leaf_count"] == 3 and plants[1]["leaf_count"] == 2
    assert all(p["box"][0] > 100 for p in plants)      # far-away green left out


def test_load_pots(tmp_path):
    layout = tmp_path / "layout.json"
    assert growth_detection.load_pots(layout) == []
    one = '{"box": [0.1, 0.5, 0.3, 0.6], "score": 0.6}'
    two = '{"box": [0.6, 0.5, 0.8, 0.6], "score": 0.5}'
    layout.write_text('{"pots": [%s, %s]}' % (one, two))
    assert growth_detection.load_pots(layout) == [[0.1, 0.5, 0.3, 0.6], [0.6, 0.5, 0.8, 0.6]]
    layout.write_text('{"pots": [%s]}' % one)                 # one of two pots found
    assert growth_detection.load_pots(layout) == []            # don't drop the other plant


def test_mirror_reflection_is_cut_at_the_contact_line():
    image = np.full((540, 960, 3), (90, 60, 70), np.uint8)       # purple-ish lid and wall
    plant = np.zeros_like(image)
    for centre, axes, angle in (((300, 200), (40, 18), 30), ((350, 240), (50, 20), -20),
                                ((320, 290), (35, 14), 10)):
        cv2.ellipse(plant, centre, axes, angle, 0, 360, LEAF, -1)
    contact = 320
    above = plant[:contact]
    reflection = (above[::-1] * 0.6).astype(np.uint8)            # dimmer, flipped copy
    image[:contact][above.any(axis=2)] = above[above.any(axis=2)]
    below = image[contact:contact + contact]
    mask = reflection[:below.shape[0]].any(axis=2)
    below[mask] = reflection[:below.shape[0]][mask]
    w, h = 960, 540
    pots = [[250 / w, 310 / h, 400 / w, 340 / h]]
    (found,) = growth_detection.find_plants(image, pots=pots)
    assert abs(found["mirror_y"] - contact) <= 6
    assert found["box"][1] + found["box"][3] <= contact + 6
    assert found["reflection_frac"] > 0



def test_white_light_tells_the_flash_from_the_purple_grow_light():
    import cv2
    import numpy as np
    import growth_detection

    white = np.full((60, 80, 3), (235, 225, 220), np.uint8)      # BGR, slightly blue-white wall
    purple = np.full((60, 80, 3), (230, 40, 200), np.uint8)      # BGR magenta-purple
    ok, a_white = growth_detection.white_light(white)
    bad, a_purple = growth_detection.white_light(purple)
    assert ok and not bad
    assert a_white < growth_detection.MAGENTA_A_MAX < a_purple
    dark = np.zeros((10, 10, 3), np.uint8)
    assert growth_detection.white_light(dark)[0] is False
