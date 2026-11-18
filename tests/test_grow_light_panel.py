"""Unit checks for the grow light photoperiod card."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from grow_light_panel import card_html, headline, photoperiod  # noqa: E402
from sensor_ingest import normalize_reading, reading_to_state  # noqa: E402

# 21:51 UTC: nine minutes before the 22:00 UTC ON start (08:00 AEST).
NOW = datetime(2026, 9, 30, 21, 51, 0, tzinfo=timezone.utc)
SENT = (NOW - timedelta(seconds=20)).isoformat()


def _gl(**extra):
    base = {"on": False, "want_on": False, "mode": "off", "on_hours": 16.0,
            "start_utc": 22.0, "off_utc": 14.0, "clock": True,
            "next_change_s": 9 * 60 + 20}
    base.update(extra)
    return base


def test_off_before_morning_start_says_back_on():
    sched = photoperiod(_gl(), SENT, NOW)
    assert sched["want_on"] is False
    assert sched["next_change"] == datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
    kind, title, detail = headline(_gl(), sched, True)
    assert kind == "off"
    assert "dark period" in title
    assert "Back on at" in detail and "9 min" in detail


def test_schedule_rolls_forward_between_messages():
    # Ten minutes later the ON window has started even without a new message.
    sched = photoperiod(_gl(), SENT, NOW + timedelta(minutes=10))
    assert sched["want_on"] is True
    assert sched["next_change"] == datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def test_old_firmware_places_clock_schedule_from_start_hour():
    gl = _gl(want_on=None, off_utc=None, next_change_s=None)
    sched = photoperiod(gl, SENT, NOW)
    assert sched["want_on"] is False
    assert sched["next_change"] == datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)


def test_uptime_schedule_without_switch_time_cannot_be_placed():
    gl = _gl(clock=False, want_on=None, next_change_s=None)
    assert photoperiod(gl, SENT, NOW) is None
    kind, _, detail = headline(gl, None, True)
    assert kind == "off" and "no NTP" in detail


def test_on_windows_cover_16_hours_of_the_local_day():
    sched = photoperiod(_gl(), SENT, NOW)
    lit = sum((end - start for start, end in sched["windows"]), timedelta())
    # A local day on a fixed offset always overlaps exactly 16 h of ON.
    assert lit == timedelta(hours=16)


def test_mismatch_is_flagged_when_live():
    gl = _gl(on=False, want_on=True, next_change_s=3600)
    kind, title, _ = headline(gl, photoperiod(gl, SENT, NOW), True)
    assert kind == "warn" and "schedule says on" in title


def test_card_glows_only_when_lit():
    assert "gl-on" not in card_html(_gl(), SENT, True, NOW).split("<div class=\"gl-card")[1][:20]
    lit = _gl(on=True, want_on=True, mode="on", next_change_s=3600)
    assert 'class="gl-card gl-on"' in card_html(lit, SENT, True, NOW)


def test_ingest_groups_grow_light_fields():
    payload = {
        "device_id": "garden-node-01", "ambient_temp_c": 26.0, "ambient_humidity_pct": 54.0,
        "water_temp_c": 23.3, "light_adc": 2679, "water_level_pct": 50, "tds_ppm": 1115,
        "ph": 6.0, "co2_ppm": 900,
        "grow_light_on": False, "grow_light_mode": "off", "grow_light_photoperiod_h": 16,
        "grow_light_schedule_start_utc": 22, "grow_light_schedule_off_utc": 14,
        "grow_light_ntp_schedule": True, "grow_light_want_on": False,
        "grow_light_next_change_s": 560,
    }
    state = reading_to_state(normalize_reading(payload), {})
    gl = state["grow_light"]
    assert gl["want_on"] is False and gl["clock"] is True
    assert gl["next_change_s"] == 560 and gl["on_hours"] == 16


# 14:30 UTC: half an hour into the dark period, inside the make-up slot.
NIGHT = datetime(2026, 9, 30, 14, 30, 0, tzinfo=timezone.utc)


def _night_gl(**extra):
    base = _gl(next_change_s=7.5 * 3600, makeup_max_night_s=7200.0)
    base.update(extra)
    return base


def test_makeup_lit_after_off_is_not_a_fault():
    gl = _night_gl(on=True, mode="on", makeup=True, makeup_armed=True, makeup_owed_s=5400.0)
    sched = photoperiod(gl, NIGHT.isoformat(), NIGHT)
    assert sched["want_on"] is False
    kind, title, detail = headline(gl, sched, True)
    assert kind == "on"
    assert "making up missed light" in title
    assert "1 h 30 min still owed" in detail


def test_owed_light_shows_in_card_until_repaid():
    gl = _night_gl(makeup_owed_s=3 * 3600.0, makeup_armed=False)
    html_out = card_html(gl, NIGHT.isoformat(), True, NIGHT)
    assert "3 h of scheduled light was missed" in html_out
    assert card_html(_night_gl(makeup_owed_s=0.0), NIGHT.isoformat(), True, NIGHT).count(
        "missed") == 0


def test_ingest_reads_makeup_fields():
    reading = normalize_reading({
        "device_id": "garden-node-01", "ambient_temp_c": 26.0, "ambient_humidity_pct": 54.0,
        "water_temp_c": 23.3, "light_adc": 2679, "water_level_pct": 50, "tds_ppm": 1115,
        "ph": 6.0, "co2_ppm": 900, "grow_light_on": True, "grow_light_mode": "on",
        "grow_light_makeup": True, "grow_light_makeup_armed": True,
        "grow_light_makeup_owed_s": 1800, "grow_light_makeup_max_night_s": 7200,
    })
    gl = reading_to_state(reading, {})["grow_light"]
    assert gl["makeup"] is True and gl["makeup_armed"] is True
    assert gl["makeup_owed_s"] == 1800 and gl["makeup_max_night_s"] == 7200


def test_last_on_window_in_the_dark_is_the_one_that_ended():
    from grow_light_panel import last_on_window
    # NOW is 21:51 UTC, in the dark period: the last ON window was 22:00 -> 14:00.
    start, end, which = last_on_window(_gl(), SENT, NOW)
    assert which == "last"
    assert start == datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
    later = NOW + timedelta(hours=2)  # 23:51, in the next ON window
    start, end, which = last_on_window(_gl(), SENT, later)
    assert which == "current" and end == later
    assert start == datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)


def test_missed_light_counts_telemetry_gaps():
    from grow_light_panel import missed_light_s
    start = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=16)
    every_10s = lambda a, b: [a + timedelta(seconds=s)  # noqa: E731
                              for s in range(0, int((b - a).total_seconds()), 10)]
    # Running all period (from before it started): nothing missed.
    times = every_10s(start - timedelta(minutes=5), end + timedelta(minutes=5))
    assert missed_light_s(times, start, end) < 60
    # Off 02:00 -> 09:00 UTC: 7 h missed.
    off_from, off_to = start + timedelta(hours=4), start + timedelta(hours=11)
    times = [t for t in times if not off_from <= t < off_to]
    assert abs(missed_light_s(times, start, end) - 7 * 3600) < 60
    # Off from before the start until 3 h in, then running: 3 h missed.
    times = every_10s(start + timedelta(hours=3), end)
    assert abs(missed_light_s(times, start, end) - 3 * 3600) < 60
    # No data at all: the whole period.
    assert missed_light_s([], start, end) == 16 * 3600


def test_manual_on_fields_from_telemetry():
    from sensor_ingest import normalize_reading, reading_to_state
    reading = normalize_reading({
        "device_id": "garden-node-01", "ambient_temp_c": 26.0, "ambient_humidity_pct": 54.0,
        "water_temp_c": 23.3, "light_adc": 2679, "water_level_pct": 50, "tds_ppm": 1115,
        "ph": 6.0, "co2_ppm": 900,
        "grow_light_on": True, "grow_light_manual": True, "grow_light_manual_left_s": 5400})
    gl = reading_to_state(reading, {})["grow_light"]
    assert gl["manual"] is True and gl["manual_left_s"] == 5400
