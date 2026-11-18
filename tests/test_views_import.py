"""Every page module imports: a syntax error in a view takes the whole app down."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VIEWS = sorted(p.stem for p in (ROOT / "src" / "views").glob("*.py") if p.stem != "__init__")


@pytest.mark.parametrize("name", VIEWS)
def test_view_imports(name):
    importlib.import_module(f"views.{name}")
