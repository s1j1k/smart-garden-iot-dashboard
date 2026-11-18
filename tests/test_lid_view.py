"""Lid view: which plant is in each net pot, and the reservoir's plant list following it."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import garden_settings  # noqa: E402
import lid_view  # noqa: E402


def test_pots_default_and_follow_plants(tmp_path, monkeypatch):
    path = tmp_path / "garden_settings.json"
    original_load, original_save = garden_settings.load, garden_settings.save
    monkeypatch.setattr(garden_settings, "load", lambda p=path: original_load(p))
    monkeypatch.setattr(garden_settings, "save", lambda updates, p=path: original_save(updates, p))

    assert lid_view.pot_plants() == ["basil", "lettuce"]          # basil left, lettuce right
    assert lid_view.set_pot(0, "spinach") == ["spinach", "lettuce"]
    stored = garden_settings.load()
    assert stored["pots"] == ["spinach", "lettuce"] and stored["plants"] == ["spinach", "lettuce"]
    lid_view.set_pot(1, lid_view.EMPTY)
    assert garden_settings.load()["plants"] == ["spinach"]
    lid_view.set_pot(1, "spinach")                                 # same plant twice: listed once
    assert garden_settings.load()["plants"] == ["spinach"]


def test_old_settings_without_pots_use_plants():
    assert lid_view.pot_plants({"plants": ["lettuce"]}) == ["lettuce", ""]
