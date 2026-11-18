"""Grow stage page: where the plants are, what to look for next, and when to switch."""

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import explain
from camera_paths import LATEST_IMAGE
import development
import garden_settings
from camera_view import crop_conditions, forest_predictions, load_growth_frame
from charts import stage_chart
from config import (
    GROW_PRESETS,
    STAGE_ORDER,
    TRANSITIONAL_STRENGTH_FRACTION,
    now_local,
    scale_tds_band_for_stage,
)
from core import (
    CHART_REFRESH_S,
    LIVE_REFRESH_S,
    drain_mqtt,
    live_context,
    publish_cal_cmd,
    publish_cmd,
    run_live,
)
from growth_targets import (
    GROWTH_STAGES,
    STAGE_LABELS,
    actual_timeline,
    hint_for,
    predicted_timeline,
    stage_change_due,
)
from plant_catalogue import (
    format_ph_target,
    load_plants,
    mqtt_setpoints_payload,
    resolve_setpoints_by_ids,
    suggest_compatible_for_selection,
)
from sqlite_store import load_stage_changes
from fmt_range import fmt_range
from admin_auth import is_admin_site
from lid_view import render_lid


STAGE_EMOJI = {
    "idle": "💤",
    "sensor_baseline": "📊",
    "filling": "💧",
    "ph_baseline": "🧪",
    "seedling": "🌱",
    "transitional": "🌿",
    "vegetative": "🥬",
    "harvest": "🧺",
}

PLANT_EMOJI = {
    "basil": "🌿",
    "lettuce": "🥬",
    "spinach": "🍃",
    "pak_choi": "🥬",
    "parsley": "🌱",
    "sage": "🌿",
    "cucumber": "🥒",
    "tomato": "🍅",
}


def plant_emoji(plant_id: str) -> str:
    return PLANT_EMOJI.get(plant_id, "🪴")


def _band_caption(band, label):
    if band.zero_width:
        text = f"{label} {band.min_value:.2f} (point)"
    else:
        text = f"{label} {fmt_range(band.min_value, band.max_value, 2)}"
    if band.unit:
        text = f"{text} {band.unit}"
    return text


def render_plant_catalogue(has_live_data):
    st.caption(
        "OSU hydroponic EC/pH guide (read-only JSON). The plants in the lid's net "
        "pots set the targets; resolved bands use overlap when possible, otherwise a midpoint "
        "average (flagged). Apply sends numeric EC/pH setpoints to the ESP32 — "
        "no catalogue logic runs on-device."
    )

    try:
        catalogue = load_plants()
    except FileNotFoundError:
        st.error(
            f"Missing `{Path('data') / 'plant_catalogue.json'}`. "
            "Place the catalogue under the dashboard `data/` folder."
        )
        return
    except Exception as exc:  # noqa: BLE001 — show loader errors in UI
        st.error(f"Failed to load plant catalogue: {exc}")
        return

    st.markdown("**Which plant is in each net pot?** Hover over a pot and click to change it.")
    pots = render_lid(catalogue, plant_emoji, editable=True)
    selected_ids = list(dict.fromkeys(p for p in pots if p))

    if not selected_ids:
        st.info("Put a plant in at least one pot to resolve a shared EC / pH target band.")
        return

    resolved = resolve_setpoints_by_ids(selected_ids, catalogue)
    anchor, suggestions = suggest_compatible_for_selection(selected_ids, catalogue, limit=3)

    c_ec, c_ph, c_ppm = st.columns(3)
    with c_ec:
        st.metric(
            "⚡ Target EC",
            (
                f"{resolved.ec_mscm.min_value:.2f}"
                if resolved.ec_mscm.zero_width
                else fmt_range(resolved.ec_mscm.min_value, resolved.ec_mscm.max_value, 2)
            ),
            delta=resolved.ec_mscm.confidence,
            delta_color="off",
        )
        if resolved.ec_mscm.confidence == "fallback_average":
            st.warning("EC confidence: **fallback_average** (no overlapping range)")
        else:
            st.success("EC confidence: **overlap**")
            if resolved.ec_mscm.zero_width:
                st.caption("Zero-width EC band (single-point overlap).")
    with c_ph:
        st.metric(
            "🧪 pH setpoint",
            f"{resolved.ph_setpoint:.1f}",
            delta=f"leave alone {fmt_range(resolved.ph_dead_low, resolved.ph_dead_high, 1)}",
            delta_color="off",
            help=(
                "The setpoint is the middle of the plants' shared optimum. The "
                "controller only doses when pH leaves the wider range the plants "
                "grow normally in: plants move pH themselves as they feed, and "
                "correcting every small swing would over-dose."
            ),
        )
        if resolved.ph.confidence == "fallback_average":
            st.warning("pH optimum ranges do not overlap — setpoint is their average.")
        elif resolved.ph.zero_width:
            st.caption(
                f"Optimum ranges meet only at pH {resolved.ph.min_value:.1f}, so that "
                "is used as the setpoint."
            )
        else:
            st.caption(
                f"Shared optimum pH {fmt_range(resolved.ph.min_value, resolved.ph.max_value, 1)}."
            )
        if resolved.ph_dead_widened:
            st.caption("Tolerance overlap was narrow; widened to setpoint ± 0.3.")
    with c_ppm:
        veg_lo = resolved.ppm500.min_value
        veg_hi = resolved.ppm500.max_value
        tr_lo, tr_hi = scale_tds_band_for_stage("transitional", veg_lo, veg_hi)
        st.metric(
            "🌿 TDS vegetative (ppm500)",
            (
                f"{veg_lo:.0f}"
                if resolved.ppm500.zero_width
                else fmt_range(veg_lo, veg_hi, 0)
            ),
            delta=resolved.ppm500.confidence,
            delta_color="off",
        )
        st.caption(
            f"Doses below {resolved.ec_dead_low:.0f} ppm; never doses above "
            f"{resolved.ec_dead_high:.0f}. TRANSITIONAL "
            f"(×{TRANSITIONAL_STRENGTH_FRACTION:g} of vegetative): "
            f"**{fmt_range(tr_lo, tr_hi, 0, ' ppm')}**, same pH."
        )

    st.markdown(
        f"**Resolved (vegetative):** {_band_caption(resolved.ec_mscm, 'EC')} · "
        f"{format_ph_target(resolved)} · "
        f"{_band_caption(resolved.ppm500, 'TDS')}"
    )

    if anchor is not None:
        st.markdown(f"**Compatibility suggestions** (anchored on **{anchor.common_name}**)")
        if not suggestions:
            st.caption("No other catalogue plants overlap both EC and pH with the anchor.")
        else:
            rows = [
                {
                    "Rank": i + 1,
                    "Plant": f"{plant_emoji(s.plant.id)} {s.plant.common_name}",
                    "EC mS/cm": fmt_range(s.plant.ec_min_mscm, s.plant.ec_max_mscm, 1),
                    "pH": fmt_range(s.plant.ph_min, s.plant.ph_max, 1),
                    "Distance": f"{s.distance:.3f}",
                }
                for i, s in enumerate(suggestions)
            ]
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    payload = mqtt_setpoints_payload(resolved, stage="vegetative")
    apply_col, preview_col = st.columns([1, 2])
    with apply_col:
        if st.button(
            "Apply to VEGETATIVE stage",
            type="primary",
            width="stretch",
            disabled=not has_live_data,
            key="plant_catalogue_apply",
        ):
            # Numeric bands first, then switch the node into vegetative.
            ok_sp = publish_cmd(payload)
            ok_stage = publish_cmd({"cmd": "stage", "value": "vegetative"})
            if ok_sp and ok_stage:
                st.session_state.last_cmd_status = (
                    f"Applied plant bands + vegetative stage → `{payload}`"
                )
            time.sleep(1.5)
            drain_mqtt()
            st.rerun()
    with preview_col:
        st.code(payload, language="json")
        if not has_live_data:
            st.caption("Waiting for ESP32 telemetry before applying setpoints.")


def stage_name(stage):
    if not stage:
        return "Not set"
    return f"{STAGE_EMOJI.get(stage, '🪴')} {STAGE_LABELS.get(stage, stage)}"


def _local_naive(ts):
    """ISO UTC text / aware datetime → naive local time for charts."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().replace(tzinfo=None)


def _stage_summary():
    ctx = live_context()
    stage, suggested = ctx["stage"], ctx["suggested_stage"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Stage on the controller", stage_name(stage))
    c2.metric("Suggested by plant age", stage_name(suggested),
              help="Week 1 seedling · weeks 2–3 transitional · weeks 4–6 vegetative · "
                   "week 7+ harvest. A guide only — the plants have the final say.")
    c3.metric("Plant age", ctx["age_label"],
              help=f"Planted {ctx['planted_at']:%d %b %Y %H:%M}. Change it in Settings.")
    if stage in (None, "idle") and ctx["plant_week"] > 0:
        st.warning("The controller is **idle**, so pH, nutrient and water are not being "
                   f"managed. Choose **{stage_name(suggested)}** below and apply it.")
    elif stage_change_due(stage, suggested):
        st.info(f"⏰ **Time to check whether the plants are ready for "
                f"{stage_name(suggested)}.** Compare the photo with the checklist below; "
                "change the stage when most of it is true. Nothing changes by itself.")
    else:
        st.success(f"On track: {stage_name(stage)} matches the plants' age.")


def _hints_and_photo():
    ctx = live_context()
    hint = hint_for(ctx["stage"])
    text_col, photo_col = st.columns([1.1, 1])
    with text_col:
        explain.heading(f"#### Ready for {stage_name(hint.next_stage)}?", "stage_ready")
        st.caption(hint.when)
        st.markdown("\n".join(f"- {item}" for item in hint.look_for))
        st.caption(f"**What changes:** {hint.then}")
    with photo_col:
        frame = load_growth_frame()
        if LATEST_IMAGE.exists():
            caption = "Latest photo"
            if not frame.empty:
                latest = frame.iloc[-1]
                caption = (f"Latest photo · {latest['timestamp']:%d %b %H:%M} · "
                           f"green coverage {latest['green_percentage']:.1f} %")
            st.image(str(LATEST_IMAGE), caption=caption, width="stretch")
        else:
            st.info("No camera photo yet — start the timelapse on the Camera page to compare "
                    "growth with the checklist.")


def _timeline():
    ctx = live_context()
    now = now_local()
    planted = _local_naive(ctx["planted_at"])
    changes = [(_local_naive(ts), stage) for ts, stage in load_stage_changes(
        st.session_state.config.get("sqlite_path") or "")]
    if not changes and ctx["stage"]:
        changes = [(now, ctx["stage"])]
    model = _development(ctx)
    slowest = max(model, key=lambda c: model[c]["harvest"]["due"]) if model else None
    start = min([planted] + [t for t, _ in changes[:1]]) - pd.Timedelta(days=1)
    end = max(now, planted + pd.Timedelta(days=56)) + pd.Timedelta(days=1)
    if slowest:
        end = max(end, model[slowest]["harvest"]["latest"] + pd.Timedelta(days=2))
        predicted = [(s["due"], s["stage"]) for s in model[slowest]["stages"]]
        label = f"Predicted from conditions ({slowest})"
    else:
        predicted, label = predicted_timeline(planted, end), "Suggested by plant age"
    chart = stage_chart(predicted, actual_timeline(changes, now), now,
                        start=start, end=end, predicted_label=label)
    explain.heading("#### ✨ Predicted vs actual stage", "stage_timeline")
    st.caption("Dashed: when each stage is due, counting each day by how good its light and "
               "temperature were for the slower crop. Solid: the stage the controller was really "
               "on (dots mark each change).")
    if chart is None:
        st.info("No stage history yet.")
    else:
        st.altair_chart(chart, width="stretch")
    if model:
        _why(model)
    frame = load_growth_frame()
    if not frame.empty:
        st.caption("🌿 Camera green coverage — a rising curve means the canopy is filling in.")
        st.line_chart(frame.set_index("timestamp")[["green_percentage"]], height=160)


def _development(ctx) -> dict:
    """{crop: {"summary", "stages", "harvest"}} from the development model."""
    settings = garden_settings.load()
    crops = tuple(sorted(set(settings.get("pots") or settings.get("plants") or [])))
    if not crops:
        return {}
    daily = crop_conditions(st.session_state.config.get("sqlite_path") or "",
                            ctx["planted_at"].isoformat(), crops, str(settings.get("breaks") or []),
                            pd.Timestamp(now_local()).floor("1h"), settings.get("grow_light_ppfd"))
    out = {}
    for crop, table in daily.items():
        summary = development.summarise(table)
        stages = development.predict_stages(table, summary, now_local())
        out[crop] = {"summary": summary, "stages": stages,
                     "harvest": [s for s in stages if s["stage"] == "harvest"][0]}
    return out


def _why(model: dict) -> None:
    explain.heading("#### ✨ Why the plants are where they are", "stage_why")
    for crop, item in model.items():
        summary = item["summary"]
        st.markdown(f"**{crop.title()}.** {development.explain(summary)}")
        rows = [{"Stage": STAGE_LABELS.get(s["stage"], s["stage"]),
                 "Due": f"{s['due']:%d %b}",
                 "Range": f"{s['earliest']:%d %b} – {s['latest']:%d %b}"}
                for s in item["stages"] if s["ideal_day"] > 0]
        cols = st.columns([2, 1])
        cols[0].dataframe(pd.DataFrame(rows).set_index("Stage"), width="stretch")
        lost = {**summary["lost"], **{k: float(v) for k, v in summary["unmeasured"].items()}}
        cols[1].dataframe(pd.Series(lost, name="Days").round(1).to_frame(), width="stretch")
    first = next(iter(model.values()))["summary"]
    light, daily_dli = first["light_hours_mean"], first["dli_mean"]
    if light == light:
        text = (f"Grow light seen on {light:.1f} h a day on average (target 16 h), from the "
                "light sensor, or the light's on/off command on days the sensor was dead.")
        if daily_dli == daily_dli:
            text += (f" Daily light integral {daily_dli:.1f} mol/m²/day (lettuce wants about "
                     f"{development.DLI_TARGET['lettuce']:.0f}, basil "
                     f"{development.DLI_TARGET['basil']:.0f}).")
        else:
            text += (" The light's brightness at the plants has not been measured, so only its "
                     "hours count; enter a PPFD reading in the garden settings to use the daily "
                     f"light integral (lettuce needs about {development.ppfd_needed('lettuce'):.0f} "
                     "µmol/m²/s for 16 h).")
        st.caption(text)


def _forest_box():
    """Both random forests' predictions in one marked box."""
    with st.container(border=True):
        explain.heading("#### 🌲 Random forest predictions ✨", "stage_forest")
        result = forest_predictions()
        stage, harvest_result = result["stage"], result["harvest"]
        left, right = st.columns(2)
        with left:
            st.markdown("**Growth stage**")
            if stage.get("ready"):
                st.metric("The forest says", stage_name(stage["prediction"]),
                          help=f"{stage['confidence'] * 100:.0f} % of the trees agree.")
                accuracy = stage.get("accuracy")
                baseline = stage.get("baseline_accuracy")
                if accuracy == accuracy:
                    st.caption(f"Right on {accuracy * 100:.0f} % of hours from days it never saw"
                               + (f" (plant age alone: {baseline * 100:.0f} %)."
                                  if baseline is not None and baseline == baseline else "."))
            else:
                st.info("🌲 " + stage.get("reason", "Not enough data yet."))
        with right:
            st.markdown("**Harvest date**")
            if harvest_result.get("ready"):
                for number, pot in harvest_result["pots"].items():
                    dates = [pot[k] for k in ("date", "earliest", "latest")]
                    if dates[0] is None:
                        text = "not growing towards harvest size yet"
                    elif None in dates:
                        text = f"{dates[0]:%d %b}"
                    else:
                        text = f"{dates[0]:%d %b} (range {dates[1]:%d %b} – {dates[2]:%d %b})"
                    st.markdown(f"🌲 **Pot {number} ({pot['crop']}):** {text}")
            else:
                st.info("🌲 " + harvest_result.get("reason", "Not enough data yet."))
        st.caption("Learned from this garden's own photos, water and nutrient use, and the "
                   "stages the controller ran. Suggestions only: the stage changes when you apply "
                   "it.")


def _breaks_editor():
    """Admin: periods the plants were out of the tank (rebuilds), so the model does
    not read an empty tank as growing conditions."""
    settings = garden_settings.load()
    breaks = list(settings.get("breaks") or [])
    st.caption("Times the plants were out of the tank, for example while the housing was "
               "rebuilt. These days count as unmeasured in the stage and harvest forecasts.")
    if breaks:
        st.dataframe(pd.DataFrame(breaks), width="stretch", hide_index=True)
    c1, c2, c3 = st.columns(3)
    start = c1.date_input("From", key="break_start")
    end = c2.date_input("Until (back in the tank)", key="break_end")
    reason = c3.text_input("Why", value="housing rebuild", key="break_reason")
    a, b = st.columns(2)
    if a.button("Add break", key="break_add", disabled=not (start and end and end > start)):
        breaks.append({"start": f"{start}T00:00", "end": f"{end}T00:00", "reason": reason})
        garden_settings.save({"breaks": breaks})
        st.cache_data.clear()
        st.rerun()
    if breaks and b.button("Remove the last break", key="break_remove"):
        garden_settings.save({"breaks": breaks[:-1]})
        st.cache_data.clear()
        st.rerun()


def _change_stage():
    ctx = live_context()
    st.markdown("#### Change stage")
    current = ctx["stage"]
    default = ctx["suggested_stage"] if stage_change_due(current, ctx["suggested_stage"])         else current
    show_setup = st.toggle("Show set-up stages (filling, baselines)", value=False,
                           key="stage_show_setup")
    options = STAGE_ORDER if show_setup else ["idle"] + GROWTH_STAGES
    if default not in options:
        default = "idle"
    selected = st.selectbox(
        "Stage", options=options, index=options.index(default), key="stage_select",
        format_func=lambda key: f"{stage_name(key)} — {GROW_PRESETS[key]['label'].split('—')[-1].strip()}")
    preview = GROW_PRESETS[selected]
    feed = (f"nutrient {fmt_range(preview['nutrient_tds_min'], preview['nutrient_tds_max'], 0, ' ppm')}"
            if preview["ec_control_enabled"] else "no nutrient dosing")
    st.caption(f"pH {preview['ph_center']:.1f} (acceptable "
               f"{fmt_range(preview['ph_dead_low'], preview['ph_dead_high'], 1)}) · {feed} · water "
               f"{preview['target_drawdown_mm']} mm below the pots")
    if st.button("Apply stage to the controller", type="primary", key="stage_apply",
                 disabled=not ctx["node_live"]):
        publish_cal_cmd({"cmd": "stage", "value": selected}, wait_seconds=1.5)
    if not ctx["node_live"]:
        st.caption("Waiting for the controller to come online before a stage can be applied.")
    elif st.session_state.get("last_cmd_status"):
        st.caption(st.session_state.last_cmd_status)
    st.caption("The stage is saved on the controller and survives power cuts.")


def page():
    explain.heading("🪴 Grow stage", "stage", "title")
    st.caption("✨ marks smart features: predictions and assessments the garden makes from "
               "its own data. 🌲 marks the random forests.")
    run_live(_stage_summary, LIVE_REFRESH_S)
    st.divider()
    run_live(_forest_box, CHART_REFRESH_S)
    st.divider()
    _hints_and_photo()
    st.divider()
    run_live(_timeline, CHART_REFRESH_S)
    st.divider()
    if not is_admin_site():
        return  # changing the stage or plant targets is admin-only
    run_live(_change_stage, LIVE_REFRESH_S)
    with st.expander("⏸️ Breaks — when the plants were out of the tank"):
        _breaks_editor()
    with st.expander("🌱 Plant targets — set pH and nutrient bands from the plant list"):
        render_plant_catalogue(live_context()["has_live_data"])
