"""Camera flash: light on → photo → light off, and never blocking a photo."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import camera_flash  # noqa: E402


def _fake_send(log, answers):
    def send(payload, want_ack):
        log.append(payload["action"])
        result = answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    return send


def _use(monkeypatch, send):
    """Stand in for the MQTT connection; ``send`` handles both "on" and "off"."""
    @contextmanager
    def sender():
        yield send
    monkeypatch.setattr(camera_flash, "_sender", sender)


def test_flash_wraps_the_photo(monkeypatch):
    log = []
    _use(monkeypatch, _fake_send(log, [True, True]))
    monkeypatch.setattr(camera_flash.time, "sleep", lambda s: log.append(f"settle {s}"))
    with camera_flash.grow_light_flash(log=lambda m: None) as lit:
        log.append("photo")
    assert lit is True
    assert log == ["on", f"settle {camera_flash.SETTLE_S}", "photo", "off"]


def test_no_ack_still_takes_photo_and_skips_off(monkeypatch):
    log, messages = [], []
    _use(monkeypatch, _fake_send(log, [False]))
    with camera_flash.grow_light_flash(log=messages.append) as lit:
        log.append("photo")
    assert lit is False and log == ["on", "photo"]
    assert "without it" in messages[0]


def test_broker_error_never_blocks_photo(monkeypatch):
    log = []
    _use(monkeypatch, _fake_send(log, [OSError("refused")]))
    with camera_flash.grow_light_flash(log=lambda m: None):
        log.append("photo")
    assert log == ["on", "photo"]


def test_broker_unreachable_never_blocks_photo(monkeypatch):
    @contextmanager
    def sender():
        raise OSError("connection refused")
        yield
    monkeypatch.setattr(camera_flash, "_sender", sender)
    with camera_flash.grow_light_flash(log=lambda m: None) as lit:
        pass
    assert lit is False


def test_flash_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CAMERA_FLASH", "0")
    monkeypatch.setattr(camera_flash, "_sender", lambda: (_ for _ in ()).throw(AssertionError))
    with camera_flash.grow_light_flash() as lit:
        pass
    assert lit is False


def test_viewfinder_keeps_light_white(monkeypatch):
    log = []
    _use(monkeypatch, _fake_send(log, [True, True, True]))
    clock = [100.0]
    monkeypatch.setattr(camera_flash.time, "monotonic", lambda: clock[0])
    with camera_flash.grow_light_held(log=lambda m: None) as keep_lit:
        keep_lit()                                  # too soon: nothing sent
        clock[0] += camera_flash.REFRESH_S
        keep_lit()                                  # re-sends "on"
    assert log == ["on", "on", "off"]
    assert camera_flash.REFRESH_S * 1000 < camera_flash.FLASH_MS


def test_viewfinder_without_light_sends_nothing_more(monkeypatch):
    log = []
    _use(monkeypatch, _fake_send(log, [False]))
    clock = [100.0]
    monkeypatch.setattr(camera_flash.time, "monotonic", lambda: clock[0])
    with camera_flash.grow_light_held(log=lambda m: None) as keep_lit:
        clock[0] += 60
        keep_lit()
    assert log == ["on"]


def test_ack_matching():
    assert camera_flash._is_flash_ack('{"cmd":"light_flash","flash":true}', True)
    assert not camera_flash._is_flash_ack('{"cmd":"light_flash","flash":false}', True)
    assert not camera_flash._is_flash_ack('{"cmd":"cycle"}', True)
    assert not camera_flash._is_flash_ack("not json", True)


def test_off_ack_tells_whether_it_is_night():
    camera_flash._last_ack.clear()
    assert camera_flash.light_mode_after_flash() is None
    assert camera_flash._is_flash_ack('{"cmd":"light_flash","flash":true,"mode":"flash"}', True)
    assert camera_flash.light_mode_after_flash() is None      # still flashing
    assert camera_flash._is_flash_ack('{"cmd":"light_flash","flash":false,"mode":"off"}', False)
    assert camera_flash.light_mode_after_flash() == "off"
    assert not camera_flash._is_flash_ack('{"cmd":"cycle"}', False)
    assert camera_flash.light_mode_after_flash() == "off"     # unrelated message kept it


def test_fixed_exposure_caller_skips_the_settle(monkeypatch):
    log = []
    _use(monkeypatch, _fake_send(log, [True, True]))
    monkeypatch.setattr(camera_flash.time, "sleep", lambda s: log.append(f"settle {s}"))
    with camera_flash.grow_light_flash(log=lambda m: None, settle_s=0):
        log.append("photo")
    assert log == ["on", "photo", "off"]


def test_night_interval(monkeypatch):
    import camera_paths
    monkeypatch.delenv("CAMERA_NIGHT_INTERVAL_S", raising=False)
    monkeypatch.setenv("CAMERA_CAPTURE_INTERVAL_S", "300")
    assert camera_paths.night_capture_interval_s() == 1800
    monkeypatch.setenv("CAMERA_NIGHT_INTERVAL_S", "60")       # never shorter than by day
    assert camera_paths.night_capture_interval_s() == 300
    monkeypatch.setenv("CAMERA_NIGHT_INTERVAL_S", "junk")
    assert camera_paths.night_capture_interval_s() == 1800
