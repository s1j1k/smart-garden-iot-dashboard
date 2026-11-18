"""One chart per measurement, each with its own unit and the band it should sit in.

Readings with different units never share an axis. Pump and light activity are
shaded behind the line they affect (water top-ups behind water level, nutrient
doses behind TDS, pH doses behind pH, grow light behind the light sensor).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import altair as alt
import pandas as pd

# Categorical slots 1–2 (validated pair), a muted shade for activity, and the
# grow light's blurple. Target bands use the line's own colour, faintly.
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
ACTIVITY = "#1baf7a"
# pH doses: up and down get their own colour and arrow so they read apart at a glance.
PH_UP = "#8b5cf6"
PH_DOWN = "#eb6834"
LIGHT = "#8b5cf6"
CHART_HEIGHT = 220

RANGES = {"6 h": 6, "24 h": 24, "3 days": 72}
# A silence longer than this (or than 4× the usual spacing) is an outage: the
# line breaks there. Shorter holes (uploads drop readings) are bridged.
MIN_GAP = timedelta(hours=2)


def local_frame(history, hours=None, now=None):
    """History rows as a DataFrame with a local-time ``t`` column, trimmed to ``hours``."""
    df = pd.DataFrame(history)
    if df.empty or "time" not in df:
        return pd.DataFrame()
    utc = pd.to_datetime(df["time"], errors="coerce")
    utc = utc.dt.tz_localize("UTC") if utc.dt.tz is None else utc.dt.tz_convert("UTC")
    local_tz = datetime.now().astimezone().tzinfo
    df["t"] = utc.dt.tz_convert(local_tz).dt.tz_localize(None)
    df = df.dropna(subset=["t"]).sort_values("t")
    if hours:
        end = (now or df["t"].max())
        df = df[df["t"] >= end - timedelta(hours=hours)]
    return df.reset_index(drop=True)


def on_periods(df, column):
    """[(start, end)] spans where ``column`` is 1; a span ends at the next reading."""
    if df.empty or column not in df:
        return []
    spans, start = [], None
    flags = pd.to_numeric(df[column], errors="coerce").fillna(0).astype(int).tolist()
    times = df["t"].tolist()
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = times[i]
        elif not flag and start is not None:
            spans.append((start, times[i]))
            start = None
    if start is not None:
        spans.append((start, times[-1]))
    return spans


def _spans_layer(spans, color, label):
    if not spans:
        return None
    frame = pd.DataFrame({"start": [s for s, _ in spans], "end": [e for _, e in spans],
                          "what": label})
    # A zero-length span (one reading) is still drawn as a thin bar.
    return alt.Chart(frame).mark_rect(color=color, opacity=0.22).encode(
        x="start:T", x2="end:T",
        tooltip=[alt.Tooltip("what:N", title=""),
                 alt.Tooltip("start:T", title="From", format="%d %b %H:%M"),
                 alt.Tooltip("end:T", title="To", format="%H:%M")])


def _band_layer(band, color):
    if not band:
        return None
    lo, hi = band
    frame = pd.DataFrame({"lo": [lo], "hi": [hi]})
    return alt.Chart(frame).mark_rect(color=color, opacity=0.10).encode(
        y="lo:Q", y2="hi:Q", tooltip=[alt.Tooltip("lo:Q", title="Target from"),
                                      alt.Tooltip("hi:Q", title="to")])


def dose_markers(events, ph_settle_s=720):
    """(nutrient, ph) marker frames [t, what, detail, color, shape] from control events.

    A nutrient dose is reported on its ec_check. A pH dose is only reported on its
    ph_recheck, which comes after the settling wait, and that wait is stretched while
    the pH is still moving (40 min on 14 Oct), so the marker goes at the ph_check that
    planned the dose. ``ph_settle_s`` is the fallback when that check is not loaded.
    """
    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 0 else None

    cols = ["t", "what", "detail", "color", "shape"]
    rows = pd.DataFrame(events or [])
    if rows.empty or "event" not in rows or "timestamp" not in rows:
        return pd.DataFrame(columns=cols), pd.DataFrame(columns=cols)
    rows = local_frame(rows.rename(columns={"timestamp": "time"}))
    settle = timedelta(seconds=float(ph_settle_s or 720))
    nutrient, ph = [], []
    last_ph_check = None
    for _, e in rows.sort_values("t", kind="stable").iterrows():
        kind = e.get("event")
        if kind == "ph_check":
            last_ph_check = e
        if kind == "ec_check" and num(e.get("ec_dose_ml")):
            before = num(e.get("ec_before"))
            nutrient.append({"t": e["t"], "what": "Nutrient dose",
                             "detail": f"{num(e['ec_dose_ml']):.1f} mL"
                                       + (f" at {before:.0f} ppm" if before else ""),
                             "color": ACTIVITY, "shape": "triangle-down"})
        elif kind == "ph_recheck" and num(e.get("ph_dose_ml")):
            up = str(e.get("ph_dose_dir")) == "up"
            planned = (last_ph_check is not None and last_ph_check["t"] < e["t"]
                       and e["t"] - last_ph_check["t"] <= timedelta(hours=2))
            at = last_ph_check["t"] if planned else e["t"] - settle
            before = num(last_ph_check.get("ph_before")) if planned else None
            ph.append({"t": at, "what": "pH-up dose ▲" if up else "pH-down dose ▼",
                       "detail": f"{num(e['ph_dose_ml']):.2f} mL"
                                 + (f" at pH {before:.2f}" if before else ""),
                       "color": PH_UP if up else PH_DOWN,
                       "shape": "triangle-up" if up else "triangle-down"})
    return pd.DataFrame(nutrient, columns=cols), pd.DataFrame(ph, columns=cols)


def _marker_layers(markers):
    """A dashed rule and a triangle at the top for each marker, in its own colour; [] if none."""
    if markers is None or markers.empty:
        return []
    tooltip = [alt.Tooltip("what:N", title=""),
               alt.Tooltip("t:T", title="At", format="%d %b %H:%M"),
               alt.Tooltip("detail:N", title="")]
    base = alt.Chart(markers).encode(x="t:T", color=alt.Color("color:N", scale=None))
    return [
        base.mark_rule(strokeWidth=1.5, strokeDash=[4, 3]).encode(tooltip=tooltip),
        base.mark_point(size=90, filled=True, opacity=1)
        .encode(y=alt.value(6), shape=alt.Shape("shape:N", scale=None), tooltip=tooltip),
    ]


def metric_chart(df, series, unit, *, band=None, spans=None, digits=1, zero=False,
                 valid=None, markers=None):
    """Line chart for one unit. ``series`` is [(column, label, colour)].

    ``spans`` is [(spans, colour, label)] of activity shaded behind the lines.
    ``markers`` is a frame from dose_markers(): one dashed line per control dose.
    ``valid`` is the (low, high) a real reading can take; anything outside is a sensor
    error sentinel (pH -9.8, water temp -127 °C, …) and is left out.
    """
    columns = [c for c, _, _ in series if c in df]
    if df.empty or not columns:
        return None
    long = df.melt(id_vars="t", value_vars=columns, var_name="col", value_name="v")
    long["v"] = pd.to_numeric(long["v"], errors="coerce")
    if valid:
        long = long[(long["v"] >= valid[0]) & (long["v"] <= valid[1])]
    long = long.dropna(subset=["v"]).sort_values(["col", "t"])
    if long.empty:
        return None
    # Number each unbroken run of readings; detail= draws each run as its own line.
    step = long.groupby("col")["t"].diff()
    gap = max(MIN_GAP, 4 * step.median()) if step.notna().any() else MIN_GAP
    long["run"] = (step > gap).groupby(long["col"]).cumsum()
    names = {c: label for c, label, _ in series}
    long["series"] = long["col"].map(names)
    domain = [names[c] for c in columns]
    colors = [color for c, _, color in series if c in columns]

    y = alt.Y("v:Q", title=unit, scale=alt.Scale(zero=zero, nice=True))
    color = alt.Color("series:N", scale=alt.Scale(domain=domain, range=colors),
                      legend=alt.Legend(orient="top", title=None) if len(domain) > 1 else None)
    x = alt.X("t:T", title=None, axis=alt.Axis(format="%d %b %H:%M", labelOverlap=True,
                                                 grid=False))
    hover = alt.selection_point(nearest=True, on="pointerover", fields=["t"], empty=False,
                                clear="pointerout")
    base = alt.Chart(long).encode(x=x)
    lines = base.mark_line(strokeWidth=2).encode(y=y, color=color, detail="run:N")
    points = base.mark_point(size=60, filled=True, opacity=0).encode(
        y=y, color=color,
        tooltip=[alt.Tooltip("t:T", title="Time", format="%d %b %H:%M"),
                 alt.Tooltip("series:N", title=""),
                 alt.Tooltip("v:Q", title=unit, format=f".{digits}f")],
    ).add_params(hover)
    rule = base.mark_rule(color="#8c959f", strokeWidth=1).encode(
        opacity=alt.condition(hover, alt.value(0.7), alt.value(0)))
    dots = base.mark_point(size=50, filled=True).encode(
        y=y, color=color, opacity=alt.condition(hover, alt.value(1), alt.value(0)))

    layers = [layer for layer in (
        _band_layer(band, colors[0]),
        *[_spans_layer(s, c, label) for s, c, label in (spans or [])],
    ) if layer is not None]
    marker_layers = []
    if markers is not None and not markers.empty:
        shown = markers[(markers["t"] >= long["t"].min()) & (markers["t"] <= long["t"].max())]
        marker_layers = _marker_layers(shown)
    layers += [lines, rule, points, dots, *marker_layers]
    return alt.layer(*layers).properties(height=CHART_HEIGHT).configure_view(stroke=None)


STAGE_AXIS = ["Set-up / idle", "Seedling", "Transitional", "Vegetative", "Harvest"]


def stage_chart(predicted, actual, now, start=None, end=None, predicted_label="Suggested by plant age"):
    """Step chart: plant-age prediction (dashed) against the stage the node was really on.

    ``predicted`` is [(start, stage)], ``actual`` is [(start, end, stage)]; times naive local.
    """
    from growth_targets import stage_rank

    def label(stage):
        return STAGE_AXIS[stage_rank(stage)]

    rows = []
    for i, (when, stage) in enumerate(predicted):
        rows.append({"t": when, "stage": label(stage), "line": predicted_label})
    if predicted:
        rows.append({"t": end or now, "stage": label(predicted[-1][1]), "line": predicted_label})
    changes = []
    for seg_start, seg_end, stage in actual:
        rows.append({"t": seg_start, "stage": label(stage), "line": "Actual stage on the node"})
        rows.append({"t": seg_end, "stage": label(stage), "line": "Actual stage on the node"})
        changes.append({"t": seg_start, "stage": label(stage), "what": f"Changed to {stage}"})
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    domain = [predicted_label, "Actual stage on the node"]
    x_scale = alt.Scale(domain=[start, end]) if start and end else alt.Undefined
    x = alt.X("t:T", title=None, scale=x_scale,
              axis=alt.Axis(format="%d %b", labelOverlap=True, grid=False))
    y = alt.Y("stage:N", title=None, sort=list(reversed(STAGE_AXIS)),
              scale=alt.Scale(domain=list(reversed(STAGE_AXIS))))
    color = alt.Color("line:N", scale=alt.Scale(domain=domain, range=["#8c959f", SERIES_1]),
                      legend=alt.Legend(orient="top", title=None))
    dash = alt.StrokeDash("line:N", scale=alt.Scale(domain=domain, range=[[6, 4], [1, 0]]),
                          legend=None)
    lines = alt.Chart(frame).mark_line(interpolate="step-after", strokeWidth=2.5).encode(
        x=x, y=y, color=color, strokeDash=dash,
        tooltip=[alt.Tooltip("line:N", title=""), alt.Tooltip("stage:N", title="Stage"),
                 alt.Tooltip("t:T", title="From", format="%d %b %H:%M")])
    layers = [lines]
    if changes:
        layers.append(alt.Chart(pd.DataFrame(changes)).mark_point(
            filled=True, size=90, color=SERIES_1, stroke="white", strokeWidth=2).encode(
            x=x, y=y, tooltip=[alt.Tooltip("what:N", title=""),
                               alt.Tooltip("t:T", title="When", format="%d %b %H:%M")]))
    layers.append(alt.Chart(pd.DataFrame({"t": [now], "what": ["Now"]})).mark_rule(
        color=SERIES_2, strokeDash=[2, 2]).encode(x=x, tooltip=alt.Tooltip("what:N", title="")))
    return alt.layer(*layers).properties(height=210).configure_view(stroke=None)
