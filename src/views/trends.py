"""Trends: one chart per measurement, each with its own unit and target band."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

import explain
from charts import ACTIVITY, LIGHT, PH_DOWN, PH_UP, RANGES, SERIES_1, SERIES_2, dose_markers, \
    local_frame, metric_chart, on_periods
from config import now_utc
from core import CHART_REFRESH_S, live_context, run_live, trend_history
from growth_targets import climate_band, climate_stage, ph_band, tds_band
from sqlite_store import load_control_events
from fmt_range import fmt_range


def _show(title, caption, chart, key):
    explain.heading(f"**{title}**", key)
    if caption:
        st.caption(caption)
    if chart is None:
        st.caption("No readings in this window.")
    else:
        st.altair_chart(chart, width="stretch")


def _band_text(band, digits, unit):
    if not band:
        return ""
    return f"Shaded band: target {fmt_range(band[0], band[1], digits, unit)}."


def _charts():
    ctx = live_context()
    state, stage = ctx["state"], ctx["stage"]
    climate = climate_stage(stage, ctx["suggested_stage"])
    hours = RANGES.get(st.session_state.get("trend_range") or "24 h", 24)
    df = local_frame(trend_history(st.session_state.config), hours)
    if not df.empty:
        df = df.drop_duplicates(subset="t", keep="last")   # live rows overlap the shared history
    if df.empty:
        st.info("No readings stored for this period yet.")
        return

    ph = ph_band(state, stage) if stage not in (None, "idle") else None
    tds = tds_band(state, stage) if stage in ("transitional", "vegetative", "harvest") else None
    pump_spans = on_periods(df, "water_pump_on")
    # Doses last ~20 s, too short for the 10 s readings to show reliably, so each
    # one is marked from the controller's own event log.
    since = (now_utc() - timedelta(hours=hours + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nutrient_marks, ph_marks = dose_markers(
        load_control_events(st.session_state.config.get("sqlite_path") or "", since),
        ph_settle_s=state.get("ph_settle_s") or 720)
    left, right = st.columns(2)
    with left:
        _show("🧪 pH", _band_text(ph, 2, "") + " Dashed lines: :violet[▲ pH-up] / "
              ":orange[▼ pH-down] doses (hover for the amount).",
              metric_chart(df, [("ph", "pH", SERIES_1)], "pH", band=ph, digits=2, valid=(0, 14),
                           markers=ph_marks,
                           spans=[(on_periods(df, "ph_up_pump_on"), PH_UP, "pH-up dose"),
                                  (on_periods(df, "ph_down_pump_on"), PH_DOWN,
                                   "pH-down dose")]), "trend_ph")
    with right:
        _show("🌿 Nutrient strength (TDS)",
              _band_text(tds, 0, " ppm") + " Green dashed lines: nutrient doses "
              "(hover for the amount).",
              metric_chart(df, [("tds_ppm", "TDS", SERIES_1)], "ppm", band=tds, digits=0, valid=(0, 3000),
                           markers=nutrient_marks,
                           spans=[(on_periods(df, "nutrient_pump_on"), ACTIVITY,
                                   "Nutrient dose")]), "trend_tds")
    left, right = st.columns(2)
    with left:
        _show("💧 Water level", "Green shading: the top-up pump was running.",
              metric_chart(df, [("water_level_pct", "Water level", SERIES_1)], "%", digits=0,
                           zero=True, valid=(0, 100), spans=[(pump_spans, ACTIVITY, "Top-up pump on")]), "trend_level")
    with right:
        air = climate_band(climate, "air_temp_c")
        _show("🌡️ Temperature", _band_text(air, 0, " °C").replace("target", "air target"),
              metric_chart(df, [("ambient_temp_c", "Air", SERIES_1),
                                ("water_temp_c", "Water", SERIES_2)], "°C", band=air,
                           valid=(-10, 60)), "trend_temp")
    left, right = st.columns(2)
    with left:
        rh = climate_band(climate, "humidity_pct")
        _show("💦 Humidity", _band_text(rh, 0, " %"),
              metric_chart(df, [("ambient_humidity_pct", "Humidity", SERIES_1)], "%", band=rh,
                           digits=0, valid=(0, 100)), "trend_humidity")
    with right:
        co2 = climate_band(climate, "co2_ppm")
        _show("💨 CO₂", _band_text(co2, 0, " ppm"),
              metric_chart(df, [("co2_ppm", "CO₂", SERIES_1)], "ppm", band=co2, digits=0,
                           valid=(250, 10000)), "trend_co2")
    _show("☀️ Light sensor", "Purple shading: the grow light was on. Higher = brighter "
          "(raw sensor value, uncalibrated).",
          metric_chart(df, [("light_adc", "Light", SERIES_1)], "sensor value", digits=0, valid=(0, 4095),
                       spans=[(on_periods(df, "led_on"), LIGHT, "Grow light on")]), "trend_light")


def page():
    explain.heading("📈 Trends", "trends", "title")
    st.segmented_control("Period", options=list(RANGES), default="24 h", key="trend_range",
                         label_visibility="collapsed")
    run_live(_charts, CHART_REFRESH_S)
