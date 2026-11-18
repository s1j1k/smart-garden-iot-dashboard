"""Camera tab: viewfinder, timelapse control, plant boxes and growth files."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import explain
import plant_growth
import timelapse_export
from admin_auth import is_admin_site
from camera_control import (
    MODE_PAUSED,
    MODE_PREVIEW,
    MODE_TIMELAPSE,
    PREVIEW_IMAGE,
    PREVIEW_TIMEOUT_S,
    camera_is_alive,
    clear_all_photos,
    load_command,
    load_status,
    preview_open,
    set_mode,
)
from services import service_states
import development
import garden_settings
import harvest
from garden_settings import planted_at
from sqlite_store import load_hourly_conditions, load_stage_changes, load_uptake_readings
from camera_paths import (
    GROWTH_CSV,
    LATEST_IMAGE,
    LATEST_PLANTS_IMAGE,
    LAYOUT_IMAGE,
    LAYOUT_JSON,
    PLANTS_LOG,
    REPO_ROOT,
    capture_interval_s,
    format_capture_interval,
)

GROWTH_COLUMNS = (
    "timestamp",
    "green_percentage",
    "plant_area",
    "sprouted",
)


def load_growth_frame(csv_path: Path = GROWTH_CSV) -> pd.DataFrame:
    """Return cleaned growth rows, or an empty frame if the file is missing.
    Read once per change of the file, shared by every viewer."""
    if not csv_path.exists():
        return pd.DataFrame(columns=list(GROWTH_COLUMNS))
    return _growth_frame(str(csv_path), csv_path.stat().st_mtime_ns)


@st.cache_data(show_spinner=False, max_entries=2)
def _growth_frame(path: str, mtime_ns: int) -> pd.DataFrame:
    csv_path = Path(path)
    frame = pd.read_csv(csv_path)
    for column in GROWTH_COLUMNS:
        if column not in frame.columns:
            return pd.DataFrame(columns=list(GROWTH_COLUMNS))
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame["green_percentage"] = pd.to_numeric(frame["green_percentage"], errors="coerce")
    frame["plant_area"] = pd.to_numeric(frame["plant_area"], errors="coerce")
    return frame.dropna(subset=list(GROWTH_COLUMNS))


def observed_capture_interval_s(frame: pd.DataFrame) -> float | None:
    """Median gap between recent growth rows, in seconds."""
    if frame.empty or len(frame) < 2:
        return None
    deltas = frame["timestamp"].sort_values().diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return None
    return float(deltas.tail(30).median())


def recent_capture_paths(frame: pd.DataFrame, limit: int = 8) -> list[Path]:
    if frame.empty or "image_path" not in frame.columns:
        return []
    found: list[Path] = []
    for raw in frame["image_path"].tail(limit):
        text = str(raw).strip()
        if not text or text.lower() == "nan":
            continue
        path = Path(text)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if path.exists():
            found.append(path)
    return found


PREVIEW_REFRESH_S = 1


@st.fragment(run_every=PREVIEW_REFRESH_S)
def _viewfinder_frame() -> None:
    """The latest viewfinder frame, refreshed on its own so the page stays still."""
    if not preview_open(load_command(), datetime.now()):
        return
    try:
        frame = PREVIEW_IMAGE.read_bytes()
        age_s = time.time() - PREVIEW_IMAGE.stat().st_mtime
    except OSError:
        st.caption("Waiting for the camera…")
        return
    st.image(frame, width="stretch")
    if age_s > 5:
        st.caption(f"⚠️ Last frame is {age_s:.0f} s old. Is the camera service running?")


def render_camera_controls(now: datetime | None = None) -> None:
    """Viewfinder to line the camera up, then start or pause the timelapse."""
    now = now or datetime.now()
    interval_s = capture_interval_s()
    interval_label = format_capture_interval(interval_s)
    command = load_command()
    status = load_status()
    alive = camera_is_alive(status, now)
    timelapse_on = command.get("mode") == MODE_TIMELAPSE
    previewing = preview_open(command, now)

    st.subheader("📷 Camera control")
    st.caption(
        "Open the viewfinder to line the camera up; its frames are never saved. "
        f"Starting the timelapse saves one still every **{interval_label}**. "
        "Run `python src/camera_capture.py` on this same machine so the buttons reach the camera."
    )

    if not alive:
        state = service_states().get("smart-garden-camera")
        why = f" (service is **{state}**)" if state else ""
        st.warning(
            f"The camera service is not responding{why}, so new photos will not be taken. "
            "On the Pi: `sudo systemctl restart smart-garden-camera` "
            "(logs: `journalctl -u smart-garden-camera -n 50`)."
        )
    elif status.get("last_error"):
        st.error(f"Last capture error: {status['last_error']}")

    if timelapse_on:
        if alive:
            next_at = status.get("timelapse_next_at") or "shortly"
            st.success(f"🎞️ Timelapse is running. Next photo: {next_at}.")
        else:
            st.info(
                "Timelapse is set to run, and it will resume when the camera script is started."
            )
        if st.button("⏸️ Pause timelapse", type="primary", width="content"):
            set_mode(MODE_PAUSED)
            st.rerun()
    elif previewing:
        st.info(
            "🔴 Viewfinder: the grow light is white and the camera refocuses as you move it. "
            "Line it up, then start the timelapse from this position. "
            f"It closes by itself after {PREVIEW_TIMEOUT_S // 60} minutes."
        )
        _viewfinder_frame()
        use_col, close_col = st.columns(2)
        with use_col:
            if st.button("✅ Use this position — start timelapse", type="primary",
                         width="stretch", disabled=not alive):
                set_mode(MODE_TIMELAPSE)
                st.rerun()
        with close_col:
            if st.button("✖️ Close viewfinder", width="stretch"):
                set_mode(MODE_PAUSED)
                st.rerun()
    else:
        st.info(
            "⏸️ Timelapse is paused. Open the viewfinder to line the camera up, "
            f"then start the timelapse ({interval_label} between frames)."
        )
        view_col, start_col = st.columns(2)
        with view_col:
            if st.button("🎥 Open viewfinder", type="primary", width="stretch",
                         disabled=not alive or bool(status.get("busy"))):
                set_mode(MODE_PREVIEW)
                st.rerun()
        with start_col:
            if st.button("▶️ Start timelapse", width="stretch",
                         disabled=not alive or bool(status.get("busy"))):
                set_mode(MODE_TIMELAPSE)
                st.rerun()


def _photo_time(name: str) -> str:
    """'2026-10-12_21-56-12.jpg' -> '12 Oct 21:56'."""
    try:
        return datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S").strftime("%d %b %H:%M")
    except ValueError:
        return name


def render_video(photos: list[Path]) -> None:
    """The latest built timelapse, and how far it reaches."""
    info = timelapse_export.video_info()
    if not info:
        return
    st.video(str(timelapse_export.VIDEO_PATH))
    behind = len(photos) - int(info.get("frames") or 0)
    st.caption(
        f"🎞️ {info.get('frames')} photos, {_photo_time(info.get('first', ''))} → "
        f"{_photo_time(info.get('last', ''))}. "
        + (f"{behind} newer photo(s) join at the next hourly update." if behind > 0 else
           "Up to date."))


def render_downloads() -> None:
    """Zip of every kept photo and an MP4 of the reel so far, built when clicked."""
    photos = timelapse_export.frames()
    st.subheader("⬇️ Download the timelapse so far")
    if not photos:
        st.caption("No timelapse photos yet. Start the timelapse to add frames to the reel.")
        return
    first, last = photos[0].stem[:16].replace("_", " "), photos[-1].stem[:16].replace("_", " ")
    st.caption(f"{len(photos)} photos, {first} → {last} — timelapse frames "
               "in the order they were taken.")
    zip_col, video_col = st.columns(2)
    with zip_col:
        # One day per zip: the whole trial is several GB, more than the Pi can
        # hold in memory while Streamlit sends it.
        days = timelapse_export.by_day(photos)
        day = st.selectbox("Day", list(reversed(days)), key="camera_zip_day",
                           format_func=lambda d: f"{d} ({len(days[d])} photos)")
        st.download_button(
            "🗂️ That day's photos (.zip)",
            data=lambda: timelapse_export.day_zip_bytes(day),
            file_name=f"smart-garden-photos-{day}.zip",
            mime="application/zip", width="stretch",
            help="Every photo at once: copy data/camera/images/ off the Pi with scp.",
        )
    with video_col:
        if timelapse_export.ffmpeg_available():
            seconds = len(photos) / timelapse_export.FPS
            st.download_button(
                f"🎞️ Timelapse video (.mp4, {seconds:.0f} s)",
                data=timelapse_export.video_bytes,
                file_name=f"smart-garden-timelapse-{photos[-1].stem[:10]}.mp4",
                mime="video/mp4", width="stretch",
                help=f"{timelapse_export.FPS} photos per second, up to the latest photo. "
                     "Kept up to date hourly in the background; a click adds any photos since.",
            )
        else:
            st.caption("Install ffmpeg on this computer to download the video "
                       "(`sudo apt install ffmpeg`).")
    render_video(photos)

    with st.expander("🗑️ Delete all photos"):
        st.warning("Deletes every timelapse photo, the growth "
                   "measurements and the video. This cannot be undone.")
        sure = st.checkbox(f"Yes, delete all {len(photos)} photos", key="camera_clear_confirm")
        if st.button("Delete all photos", disabled=not sure, key="camera_clear"):
            removed = clear_all_photos()
            st.success(f"Deleted {removed} photos.")
            st.rerun()


def render_layout(layout_json: Path = LAYOUT_JSON, layout_image: Path = LAYOUT_IMAGE) -> None:
    """What the pre-trained detector last found: the net pots that anchor each plant."""
    try:
        layout = json.loads(layout_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.caption("🤖 Pre-trained pot detector: not run yet. It needs the "
                   "smart-garden-detector service and a timelapse frame.")
        return
    pots, found = len(layout.get("pots", [])), len(layout.get("plants", []))
    with st.expander(f"🤖 Pre-trained detector: {pots} net pot(s), {found} plant(s) "
                     f"· {layout.get('located_at', '?')}"):
        if layout_image.exists():
            st.image(str(layout_image), width="stretch")
        st.caption(
            f"Grounding DINO (`{layout.get('model')}`, Apache-2.0) found these in "
            f"{layout.get('seconds', '?')} s. Each net pot holds one plant: leaves are "
            "measured against the nearest pot, and green below a pot's rim (reflections) "
            "is ignored." + ("" if pots else " No pots found, so plants are grouped by "
                                             "position instead."))
        if layout.get("error"):
            st.warning(f"Last run failed: {layout['error']}")


def render_plants(plants_log: Path = PLANTS_LOG) -> None:
    """Per-plant measurements from the plant boxes, and the camera stage estimate."""
    plants = _plants(str(plants_log), plants_log.stat().st_mtime_ns if plants_log.exists() else 0)
    st.divider()
    explain.heading("🌱 Each plant", "camera_plants", "subheader")
    unlit = _last_unlit(plants_log)
    if unlit:
        st.warning(f"Photos since {unlit} were taken without the white flash (the controller "
                   "is offline or the light did not turn white), so the plants in them were not "
                   "measured. Measurements resume with the next photo lit white.")
    render_layout()
    if plants.empty:
        st.info("No plant boxes yet. They are recorded with each timelapse frame.")
        return

    features = plant_growth.size_features(plants)
    latest = plants[plants["timestamp"] == plants["timestamp"].max()]
    table = latest.set_index("plant")[features].rename(columns=plant_growth.FEATURE_LABELS)
    by_pot = (latest.get("anchor") == "pot").all() if "anchor" in latest else False
    table.index = [f"{'Pot' if by_pot else 'Plant'} {n}" for n in table.index]
    st.caption(f"Latest frame {latest['timestamp'].iloc[0]:%d %b %H:%M}. " + (
        "One row per net pot (a pot can hold several seedlings), numbered left to right; "
        "reflections in the lid are removed." if by_pot else
        "Plants are numbered left to right. Once the pre-trained detector has found the "
        "net pots, rows become one per pot and lid reflections are removed."))
    st.dataframe(table.round(2), width="stretch")
    if features is plant_growth.FEATURES:
        st.caption("Sizes are in centimetres, measured against the net-pot rims (70 mm across, "
                   "207 mm apart), so a camera move does not read as growth. Frames where a rim "
                   "is hidden or cut off are left out.")
    else:
        st.caption("These photos were measured before the pot-rim scale existed, so sizes are "
                   "shares of the frame and jump when the camera moves. Re-measure them with "
                   "`scripts/remeasure_plants.py`.")

    area_col, height_col = st.columns(2)
    for column, feature in ((area_col, features[0]), (height_col, features[1])):
        series = plants.pivot_table(index="timestamp", columns="plant", values=feature)
        series = series.resample("1h").mean().dropna(how="all")
        series.columns = [f"Plant {n}" for n in series.columns]
        with column:
            st.markdown(f"**{plant_growth.FEATURE_LABELS[feature]}**")
            st.line_chart(series, height=200)

    explain.heading("#### 🌲 Random forest: growth stage ✨", "camera_stage")
    sqlite_path = st.session_state.config.get("sqlite_path") or ""
    changes = load_stage_changes(sqlite_path)
    since = plants["timestamp"].min() - pd.Timedelta(days=1)
    uptake = _uptake(sqlite_path, since.to_pydatetime().astimezone(timezone.utc).isoformat(),
                     pd.Timestamp.now().floor("1h"))
    planted = planted_at()
    estimate = _stage_estimate(plant_growth.hourly_features(plants, uptake), tuple(changes),
                               planted.isoformat())
    if not estimate["ready"]:
        st.caption(estimate["reason"])
    else:
        _render_stage_estimate(estimate)
    _render_harvest(plants, sqlite_path, planted)


def _render_stage_estimate(estimate: dict) -> None:
    stage_col, accuracy_col, schedule_col, data_col = st.columns(4)
    stage_col.metric("Camera suggests", estimate["prediction"].title(),
                     help=f"{estimate['confidence'] * 100:.0f} % of the trees agree.")
    accuracy_col.metric("Accuracy on unseen days", _pct(estimate["accuracy"]))
    schedule_col.metric(
        "Age schedule, same hours", _pct(estimate.get("baseline_accuracy")),
        help=(f"The stage the plants' age alone gives (now: "
              f"{(estimate.get('baseline_now') or '—').title()}), scored on the same hours. "
              "The forest is only worth more than the calendar if it beats this."))
    data_col.metric("Labelled data", f"{estimate['days']} days",
                    help=", ".join(f"{s}: {h} h" for s, h in estimate["classes"].items()))
    importance = pd.Series(estimate["importance"]).rename(plant_growth.FEATURE_LABELS)
    st.caption("What the random forest relies on most: " + ", ".join(
        f"{name} {share * 100:.0f} %" for name, share in importance.items()) + ".")
    st.caption("A suggestion only, learned from the stages you applied. It never changes "
               "the stage on the controller.")


def forest_predictions() -> dict:
    """Both random forests' latest output, for the Grow stage page:
    {"stage": estimate_stage result, "harvest": harvest.forecast result}."""
    plants = _plants(str(PLANTS_LOG), PLANTS_LOG.stat().st_mtime_ns if PLANTS_LOG.exists() else 0)
    if plants.empty:
        reason = {"ready": False, "reason": "No plant measurements yet: start the timelapse."}
        return {"stage": reason, "harvest": reason}
    sqlite_path = st.session_state.config.get("sqlite_path") or ""
    hour = pd.Timestamp.now().floor("1h")
    since = plants["timestamp"].min() - pd.Timedelta(days=1)
    uptake = _uptake(sqlite_path, since.to_pydatetime().astimezone(timezone.utc).isoformat(), hour)
    planted = planted_at()
    stage = _stage_estimate(plant_growth.hourly_features(plants, uptake),
                            tuple(load_stage_changes(sqlite_path)), planted.isoformat())
    settings = garden_settings.load()
    pots = [p for p in settings.get("pots") or settings.get("plants") or []]
    conditions = crop_conditions(sqlite_path, planted.isoformat(), tuple(sorted(set(pots))),
                                 str(settings.get("breaks") or []), hour,
                                 settings.get("grow_light_ppfd"))
    harvest_result = _harvest_forecast(harvest.daily_sizes(plants), conditions, tuple(pots), hour)
    return {"stage": stage, "harvest": harvest_result}


def _render_harvest(plants: pd.DataFrame, sqlite_path: str, planted: datetime) -> None:
    """The random-forest harvest date per pot, beside the development model's."""
    explain.heading("#### 🌲 Random forest: harvest date ✨", "camera_harvest")
    settings = garden_settings.load()
    pots = [p for p in settings.get("pots") or settings.get("plants") or []]
    conditions = crop_conditions(sqlite_path, planted.isoformat(), tuple(sorted(set(pots))),
                                 str(settings.get("breaks") or []), pd.Timestamp.now().floor("1h"),
                                 settings.get("grow_light_ppfd"))
    sizes = harvest.daily_sizes(plants)
    result = _harvest_forecast(sizes, conditions, tuple(pots), pd.Timestamp.now().floor("1h"))
    if not result["ready"]:
        st.caption(result["reason"])
    else:
        rows = []
        for number, pot in result["pots"].items():
            dates = [pot[k] for k in ("date", "earliest", "latest")]
            rows.append({
                "Pot": f"{number} ({pot['crop']})", "Leaf area now (cm²)": round(pot["area_cm2"], 1),
                "Harvest size (cm²)": pot["target_cm2"], "Growing (%/day)": round(pot["rgr_pct"], 1),
                "Harvest from canopy": "not growing" if dates[0] is None else f"{dates[0]:%d %b}",
                "Range": "—" if None in dates else f"{dates[1]:%d %b} – {dates[2]:%d %b}",
                "Harvest from conditions": _model_harvest(conditions.get(pot["crop"]), planted),
            })
        st.dataframe(pd.DataFrame(rows).set_index("Pot"), width="stretch")
        importance = pd.Series(result["importance"]).rename(harvest.FEATURE_LABELS)
        st.caption(f"Learned from {result['days']} days of growth. What sets the growth rate most: "
                   + ", ".join(f"{name} {share * 100:.0f} %" for name, share in importance.items())
                   + ".")
    st.caption("**From canopy**: a random forest learns how fast each plant's leaf area grows "
               "under the conditions it had, and runs today's size forward under recent "
               "conditions to the harvest size. **From conditions**: the development model on "
               "the Grow stage page. Harvest sizes are estimates until the first harvest.")


def _model_harvest(daily: pd.DataFrame | None, planted: datetime) -> str:
    if daily is None or daily.empty:
        return "—"
    summary = development.summarise(daily)
    stage = [s for s in development.predict_stages(daily, summary, datetime.now())
             if s["stage"] == "harvest"]
    return f"{stage[0]['due']:%d %b}" if stage else "—"


@st.cache_data(show_spinner="Reading the growing conditions…", max_entries=4)
def crop_conditions(sqlite_path: str, planted_iso: str, crops: tuple, breaks_text: str,
                    hour: pd.Timestamp, ppfd: float | None = None) -> dict:
    """{crop: development.daily_development table} from planting to now; refreshed
    hourly (``hour``) or when the planting date, crops or breaks change."""
    import ast

    planted = datetime.fromisoformat(planted_iso)
    since = planted.astimezone(timezone.utc).isoformat()
    hourly = development.hourly_conditions(load_hourly_conditions(sqlite_path, since))
    breaks = development.parse_breaks(ast.literal_eval(breaks_text) if breaks_text else [])
    start = planted.astimezone().replace(tzinfo=None) if planted.tzinfo else planted
    return {crop: development.daily_development(hourly, crop, start, datetime.now(), breaks, ppfd)
            for crop in crops}


@st.cache_data(show_spinner="Forecasting the harvest…", max_entries=2)
def _harvest_forecast(sizes: pd.DataFrame, conditions: dict, pots: tuple, hour: pd.Timestamp) -> dict:
    return harvest.forecast(sizes, conditions, list(pots), datetime.now())


@st.cache_data(show_spinner=False, max_entries=2)
def _plants(path: str, mtime_ns: int) -> pd.DataFrame:
    """plants.jsonl with growth rates, re-read only when the camera writes a frame."""
    return plant_growth.with_growth_rate(plant_growth.load_plants(Path(path)))


def _last_unlit(plants_log: Path) -> str | None:
    """Time of the first photo in the current run of unlit photos, or None when
    the latest photo was lit white (or the log is empty)."""
    if not plants_log.exists():
        return None
    first = None
    for line in reversed(plants_log.read_text(encoding="utf-8").splitlines()[-500:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("flash") is not False:
            break
        first = entry.get("timestamp")
    return first


def _pct(share) -> str:
    return "—" if share is None or share != share else f"{share * 100:.0f} %"


@st.cache_data(show_spinner="Reading the tank's water and nutrient use…", max_entries=2)
def _uptake(sqlite_path: str, since_iso: str, hour: pd.Timestamp) -> pd.DataFrame:
    """Hourly uptake features; ``hour`` makes the cache refresh once an hour."""
    return plant_growth.uptake_features(
        plant_growth.readings_frame(load_uptake_readings(sqlite_path, since_iso)))


@st.cache_data(show_spinner="Training the stage classifier…", max_entries=2)
def _stage_estimate(hourly: pd.DataFrame, changes: tuple, planted_iso: str) -> dict:
    """Retrains only when a new hour of data, a stage change or a new planting date arrives."""
    return plant_growth.estimate_stage(hourly, list(changes), datetime.fromisoformat(planted_iso))


def render_camera_tab(csv_path: Path = GROWTH_CSV, latest_image: Path = LATEST_IMAGE) -> None:
    interval_s = capture_interval_s()
    # Camera commands, downloads and deleting photos are admin-only.
    if is_admin_site():
        st.caption(
            "📷 Raspberry Pi Camera Module 3. Kept JPEGs under `data/camera/images/` "
            "are the timelapse frames. This tab does not use MQTT."
        )
        st.caption(
            "Stitch a clip with "
            "`ffmpeg -framerate 24 -pattern_type glob -i 'data/camera/images/*.jpg' "
            "-c:v libx264 -pix_fmt yuv420p timelapse.mp4`."
        )
        render_camera_controls()
        render_downloads()
    else:
        render_video(timelapse_export.frames())

    frame = load_growth_frame(csv_path)
    if not csv_path.exists() or frame.empty:
        st.info("No timelapse photos yet. Start the timelapse to take the first one.")
        return

    latest = frame.iloc[-1]
    observed = observed_capture_interval_s(frame)
    if observed is None:
        cadence = f"script default is one still every {format_capture_interval(interval_s)}"
    else:
        cadence = (
            f"recent frames are about {format_capture_interval(observed)} apart "
            f"(script default {format_capture_interval(interval_s)})"
        )
    st.caption(f"🎞️ Timelapse cadence: {cadence}.")

    explain.heading("📷 Current camera status", "camera_status", "subheader")
    image_col, metric_col = st.columns([1.5, 1])
    with image_col:
        st.markdown("**📸 Latest frame**")
        boxed = latest_image.with_name(LATEST_PLANTS_IMAGE.name)
        shown = boxed if boxed.exists() else latest_image
        if shown.exists():
            st.image(
                str(shown),
                caption=f"Captured: {latest['timestamp']}"
                        + (" · plants boxed" if shown == boxed else ""),
                width="stretch",
            )
        else:
            st.info("Latest image is not available yet.")
    with metric_col:
        st.markdown("**🌿 Growth metrics**")
        st.metric("🌿 Green coverage", f"{latest['green_percentage']:.2f}%")
        st.metric("📏 Plant area", f"{latest['plant_area']:,.0f} px")
        sprouted = str(latest["sprouted"]).strip().lower() == "true"
        if sprouted:
            st.success("🌱 Sprouted")
        else:
            st.info("🌰 Not sprouted")
        st.write(f"**🕐 Last capture:** {latest['timestamp']}")

    recent_paths = recent_capture_paths(frame)
    if len(recent_paths) >= 2:
        st.markdown("**🎞️ Recent frames** (oldest → newest)")
        st.image(
            [str(path) for path in recent_paths],
            caption=[path.name for path in recent_paths],
            width="stretch",
        )

    render_plants()

    st.divider()
    explain.heading("📈 Growth history", "camera_history", "subheader")
    history = frame.set_index("timestamp")
    st.markdown("**🌿 Green coverage over time**")
    st.line_chart(history[["green_percentage"]])
    st.markdown("**📏 Plant area over time**")
    st.line_chart(history[["plant_area"]])

    st.divider()
    explain.heading("📋 Recent camera measurements", "camera_table", "subheader")
    recent = frame.tail(10).copy()
    recent["green_percentage"] = recent["green_percentage"].round(2)
    recent["plant_area"] = recent["plant_area"].round(0)
    st.dataframe(
        recent[list(GROWTH_COLUMNS)],
        width="stretch",
        hide_index=True,
    )
    st.info(
        "Green-pixel and sprouting thresholds are prototype values. "
        "The plant region of interest will be calibrated once the camera position is final."
    )
