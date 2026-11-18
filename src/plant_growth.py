"""Per-plant growth from the camera's plant boxes, and an experimental stage estimate.

Reads ``data/camera/plants.jsonl`` (one line per timelapse frame, written by
camera_capture.py) and needs only pandas / numpy, so the dashboard does not
need OpenCV.

Features (per plant, then averaged over the plants in a frame):
    area_cm2      leaf area seen from the side, cm2
    height_cm     box height, cm
    width_cm      box width, cm
    leaf_count    leaf blades (touching leaves split at their narrow joins)
    growth_pct_day  relative growth of leaf area over the last day, % per day

Sizes are in centimetres, scaled per frame from the net-pot rims
(growth_detection.frame_scale), so a camera move or zoom does not read as
growth. Frames without a scale (no whole rim in view, or a scale more than
SCALE_JUMP from its recent median, i.e. a misdetected rim) are left out. Logs
written before the scale existed have only frame shares (area_pct, height_pct,
width_pct); those are used, with a note, until the photos are re-measured
(scripts/remeasure_plants.py).

Uptake features (from the sensor log, the same for every plant in the tank):
    water_uptake_l_day     water the plants took up (and the tank lost to
                           evaporation) over the last day: top-up water in,
                           minus the change in tank volume
    nutrient_uptake_mg_day dissolved salts that left the solution over the last
                           day: the fall in TDS x volume (ppm.L = mg), counted
                           only in hours with no nutrient dose being mixed in

Both are mass balances, so they measure the plants and not the controller: a
stage that lowers the water line or raises the nutrient strength moves the
tank's level and TDS, but the water and salts the controller adds are counted
in (water) or left out (nutrient doses), so neither shows up as uptake. The
absolute TDS, pH and the plants' age are left out for the same reason: the
controller sets TDS and pH from the stage, and the stage labels follow the
calendar closely, so those features would let the forest read the label back
instead of learning anything about the plants.

Classifier: a random forest (scikit-learn) learns any per-hour label from these
features. Growth stage is the first: each hour is labelled with the stage the
controller was really on (the sensor log's stage changes) and the forest guesses
the stage of the latest hour. Accuracy is measured leaving one whole day out at a
time, and the age-based schedule (config.suggested_grow_stage) is scored on the
same hours as the baseline to beat. It is a suggestion, not a control input: the
stage still changes only when someone applies it. Other labels (plant health,
harvest readiness) can be trained the same way with train_classifier().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from camera_paths import PLANTS_LOG

FEATURES = ["area_cm2", "height_cm", "width_cm", "leaf_count", "growth_pct_day"]
FRAME_FEATURES = ["area_pct", "height_pct", "width_pct", "leaf_count", "growth_pct_day"]
FEATURE_LABELS = {
    "area_cm2": "Leaf area (cm²)",
    "height_cm": "Height (cm)",
    "width_cm": "Width (cm)",
    "area_pct": "Leaf area (% of frame)",
    "height_pct": "Height (% of frame)",
    "width_pct": "Width (% of frame)",
    "leaf_count": "Leaves seen",
    "growth_pct_day": "Growth (% / day)",
    "water_uptake_l_day": "Water uptake (L / day)",
    "nutrient_uptake_mg_day": "Nutrient uptake (mg / day)",
}
UPTAKE_FEATURES = ["water_uptake_l_day", "nutrient_uptake_mg_day"]
ALL_FEATURES = FEATURES + UPTAKE_FEATURES
SCALE_JUMP = 0.15       # a frame's mm/px this far from the 6 h median is a misread rim
SCALE_WINDOW = "6h"
GROWTH_STAGES = ("seedling", "transitional", "vegetative", "harvest")
GROWTH_WINDOW = pd.Timedelta(days=1)
UPTAKE_WINDOW = "24h"
UPTAKE_MIN_HOURS = 12   # hours with a valid balance needed in the window
MAX_WATER_L_PER_H = 0.5  # a larger hourly change is a drain, water change or refill
MAX_NUTRIENT_MG_PER_H = 2000.0  # likewise for the salt balance (a 7 L tank at 800 ppm holds ~5600 mg)
DOSE_HOURS = 2          # the salt balance skips the hour of a dose and the next (mixing)
MIN_DAYS = 3            # labelled days needed before the estimate is shown
MIN_CLASSES = 2         # classes (e.g. stages) that must appear in the labels
TREES = 100
EVAL_TREES = 30        # smaller forests for the day-by-day accuracy check (Pi CPU time)


def load_plants(path: Path = PLANTS_LOG) -> pd.DataFrame:
    """One row per plant per frame: timestamp, plant (1, 2, …) and its measurements."""
    columns = ["timestamp", "plant", "anchor", "area_pct", "height_pct", "width_pct", "leaf_count",
               "area_cm2", "height_cm", "width_cm", "mm_per_px"]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            when = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError, TypeError):
            continue
        if entry.get("flash") is False:
            continue   # not lit white: nothing was measured
        for number, plant in enumerate(entry.get("plants") or [], start=1):
            rows.append({
                "timestamp": when,
                "plant": int(plant.get("number", number)),
                "anchor": plant.get("anchor", "position"),
                "area_pct": 100 * float(plant.get("area_frac", np.nan)),
                "height_pct": 100 * float(plant.get("height_frac", np.nan)),
                "width_pct": 100 * float(plant.get("width_frac", np.nan)),
                "leaf_count": float(plant.get("leaf_count", np.nan)),
                **{key: _num(plant.get(key)) for key in ("area_cm2", "height_cm", "width_cm",
                                                         "mm_per_px")},
            })
    if not rows:
        return pd.DataFrame(columns=columns)
    return clean_scale(pd.DataFrame(rows).sort_values(["timestamp", "plant"]).reset_index(drop=True))


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def clean_scale(plants: pd.DataFrame) -> pd.DataFrame:
    """Blank the cm measurements of frames whose scale jumps more than SCALE_JUMP
    from the median of the frames around it (a rim hidden by a cable or cut by the
    frame edge). A real camera move shifts the median within SCALE_WINDOW."""
    if plants.empty or plants["mm_per_px"].isna().all():
        return plants
    per_frame = plants.groupby("timestamp")["mm_per_px"].first().dropna()
    median = per_frame.rolling(SCALE_WINDOW, center=True, min_periods=1).median()
    bad = per_frame.index[(per_frame / median - 1).abs() > SCALE_JUMP]
    out = plants.copy()
    out.loc[out["timestamp"].isin(bad), ["area_cm2", "height_cm", "width_cm"]] = np.nan
    return out


def size_features(plants: pd.DataFrame) -> list[str]:
    """FEATURES when the log has cm measurements, else FRAME_FEATURES (old logs)."""
    has_cm = "area_cm2" in plants and plants["area_cm2"].notna().any()
    return FEATURES if has_cm else FRAME_FEATURES


def with_growth_rate(plants: pd.DataFrame, window: pd.Timedelta = GROWTH_WINDOW) -> pd.DataFrame:
    """Add growth_pct_day: each plant's relative leaf-area growth over ``window``,
    in % per day (cm2 when scaled, else the frame share: a ratio either way).

    Empty until the plant has been seen for at least half the window.
    """
    if plants.empty:
        return plants.assign(growth_pct_day=pd.Series(dtype=float))
    area = "area_cm2" if size_features(plants) is FEATURES else "area_pct"
    parts = []
    for _, one in plants.groupby("plant", sort=True):
        one = one.sort_values("timestamp")
        seen = one[["timestamp", area]].dropna().rename(columns={"timestamp": "then"})
        if seen.empty:
            parts.append(one.assign(growth_pct_day=np.nan))
            continue
        earlier = pd.merge_asof(
            pd.DataFrame({"timestamp": one["timestamp"] - window}), seen,
            left_on="timestamp", right_on="then", direction="nearest",
        )
        elapsed = (one["timestamp"].to_numpy() - earlier["then"].to_numpy()) / np.timedelta64(1, "D")
        then = earlier[area].to_numpy()
        ratio = one[area].to_numpy() / np.where(then > 0, then, np.nan)
        rate = 100 * (ratio ** (1 / np.where(elapsed > 0, elapsed, np.nan)) - 1)
        rate[elapsed < (window / pd.Timedelta(days=1)) / 2] = np.nan
        parts.append(one.assign(growth_pct_day=rate))
    return pd.concat(parts).sort_values(["timestamp", "plant"]).reset_index(drop=True)


def readings_frame(rows: list[dict]) -> pd.DataFrame:
    """Sensor-log rows (UTC ISO ``timestamp``) as a frame on naive local time, the
    clock plants.jsonl uses. Columns: tank_volume_l, tds_ppm, water_rem_ml,
    nutrient_pump_on, drain_pump_on (missing ones are NaN)."""
    columns = ["tank_volume_l", "tds_ppm", "water_rem_ml", "nutrient_pump_on", "drain_pump_on"]
    frame = pd.DataFrame(rows or [], columns=["timestamp", *columns])
    when = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce", format="ISO8601")
    # Per reading, so a daylight-saving change inside the log lands on the right hour.
    frame["timestamp"] = pd.to_datetime(
        [t.to_pydatetime().astimezone().replace(tzinfo=None) if pd.notna(t) else pd.NaT
         for t in when])
    frame[columns] = frame[columns].apply(pd.to_numeric, errors="coerce")
    return frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def uptake_features(readings: pd.DataFrame) -> pd.DataFrame:
    """Hourly water and nutrient uptake over the last day, from the sensor log.

    Water: top-up water in (the fall in the top-up bottle's remaining mL; a refill
    counts as nothing, so a top-up in the same hour as a refill is missed) minus the
    change in tank volume, per hour. Nutrient: the fall
    in TDS x volume per hour, skipped for the hour of a nutrient dose and the next.
    Hours with the drain pump on, or a change too big to be uptake, are skipped by
    both. Each feature is the mean of the valid hours in the last 24 h, times 24.
    """
    if readings.empty:
        return pd.DataFrame(columns=UPTAKE_FEATURES, index=pd.DatetimeIndex([]))
    hourly = readings.set_index("timestamp").resample("1h")
    level = hourly[["tank_volume_l", "tds_ppm", "water_rem_ml"]].median()
    pumps = hourly[["nutrient_pump_on", "drain_pump_on"]].max().fillna(0) > 0

    topped_up = (level["water_rem_ml"].shift(1) - level["water_rem_ml"]).clip(lower=0) / 1000
    water = topped_up.fillna(0) - level["tank_volume_l"].diff()
    drained = pumps["drain_pump_on"] | pumps["drain_pump_on"].shift(1, fill_value=False)
    water = water.where(~drained & (water.abs() <= MAX_WATER_L_PER_H))

    salts = level["tds_ppm"] * level["tank_volume_l"]
    nutrient = salts.shift(1) - salts
    dosing = pumps["nutrient_pump_on"].astype(int).rolling(DOSE_HOURS, min_periods=1).max() > 0
    nutrient = nutrient.where(~drained & ~dosing & water.notna()
                              & (nutrient.abs() <= MAX_NUTRIENT_MG_PER_H))

    def per_day(per_hour: pd.Series) -> pd.Series:
        return 24 * per_hour.rolling(UPTAKE_WINDOW, min_periods=UPTAKE_MIN_HOURS).mean()

    return pd.DataFrame({"water_uptake_l_day": per_day(water),
                         "nutrient_uptake_mg_day": per_day(nutrient)})


def hourly_features(plants: pd.DataFrame, uptake: pd.DataFrame | None = None) -> pd.DataFrame:
    """Plant measurements averaged over the plants and over each hour, joined with
    the tank's uptake for the same hour when ``uptake`` is given."""
    if plants.empty:
        return pd.DataFrame(columns=FEATURES)
    if "growth_pct_day" not in plants:
        plants = with_growth_rate(plants)
    per_frame = plants.groupby("timestamp")[size_features(plants)].mean()
    hourly = per_frame.resample("1h").mean().dropna(how="all")
    if uptake is not None and not uptake.empty:
        hourly = hourly.join(uptake[UPTAKE_FEATURES], how="left")
    return hourly


def area_column(hourly: pd.DataFrame) -> str:
    """The leaf-area column a feature table carries: area_cm2, else area_pct."""
    return "area_cm2" if "area_cm2" in hourly and hourly["area_cm2"].notna().any() else "area_pct"


def stage_at(times: pd.Index, changes: list[tuple[str, str]]) -> pd.Series:
    """The controller's stage at each (naive local) time, from the sensor log's
    [(UTC ISO time, stage)] changes. Set-up stages and idle become NaN."""
    if not changes or len(times) == 0:
        return pd.Series(np.nan, index=times, dtype=object)
    switched = pd.DataFrame(
        [(_local_naive(ts), stage) for ts, stage in changes], columns=["when", "stage"]
    ).sort_values("when")
    labels = pd.merge_asof(
        pd.DataFrame({"when": pd.to_datetime(times)}), switched, on="when", direction="backward"
    )["stage"]
    labels = labels.where(labels.isin(GROWTH_STAGES))
    return pd.Series(labels.to_numpy(), index=times, dtype=object)


def _local_naive(ts: str) -> pd.Timestamp:
    when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return pd.Timestamp(when.astimezone().replace(tzinfo=None))


def _forest(trees: int = TREES):
    from sklearn.ensemble import RandomForestClassifier

    # Small and shallow-leaved: a Raspberry Pi trains it in about a second,
    # and min_samples_leaf stops it memorising single noisy hours.
    return RandomForestClassifier(n_estimators=trees, min_samples_leaf=3,
                                  class_weight="balanced", random_state=0, n_jobs=1)


def _fit(features: pd.DataFrame, labels: pd.Series, trees: int = TREES):
    """Random forest on ``features``; missing values take the training median."""
    fill = features.median()
    model = _forest(trees).fit(features.fillna(fill), labels.to_numpy())
    return model, fill


def held_out_guesses(features: pd.DataFrame, labels: pd.Series) -> pd.Series:
    """The forest's guess for each hour, from a forest that never saw that hour's day.

    Hours within a day are near-copies of each other, so a random split would
    flatter the model; holding out whole days does not. Days whose removal leaves
    fewer than MIN_CLASSES labels to learn from are skipped (not in the result).
    """
    days = pd.Index(features.index.date)
    guesses = []
    for day in days.unique():
        test = np.asarray(days == day)
        if labels[~test].nunique() < MIN_CLASSES:
            continue
        train = features[~test].dropna(axis=1, how="all")
        model, fill = _fit(train, labels[~test], EVAL_TREES)
        guess = model.predict(features.loc[test, train.columns].fillna(fill))
        guesses.append(pd.Series(guess, index=features.index[test], dtype=object))
    return pd.concat(guesses) if guesses else pd.Series(dtype=object)


def leave_one_day_out(features: pd.DataFrame, labels: pd.Series) -> float:
    """Share of hours labelled right by a forest that never saw that day."""
    guesses = held_out_guesses(features, labels)
    return _agreement(guesses, labels)


def _agreement(guesses: pd.Series, labels: pd.Series) -> float:
    if guesses.empty:
        return float("nan")
    return float((guesses.to_numpy() == labels.reindex(guesses.index).to_numpy()).mean())


def age_schedule(times: pd.Index, planted_at: datetime) -> pd.Series:
    """The stage the age-based schedule (config.suggested_grow_stage) gives each
    naive local time, from the planting date. NaN before planting."""
    from config import suggested_grow_stage

    planted = pd.Timestamp(planted_at)
    if planted.tzinfo is not None:
        planted = pd.Timestamp(planted.to_pydatetime().astimezone().replace(tzinfo=None))
    days = (pd.DatetimeIndex(times) - planted) / pd.Timedelta(days=1)
    weeks = np.floor(np.asarray(days, dtype=float) / 7) + 1
    stages = [suggested_grow_stage(int(w)) if np.isfinite(w) else None for w in weeks]
    return pd.Series(stages, index=times, dtype=object)


def train_classifier(hourly: pd.DataFrame, labels: pd.Series, what: str = "label",
                     baseline: pd.Series | None = None) -> dict:
    """Train a random forest to predict ``labels`` (one per hour, NaN = unlabelled)
    from the plant features (and the uptake features when ``hourly`` has them), and
    predict the latest hour.

    Generic on purpose: growth stage is the first label; plant health, harvest
    readiness or a nutrient problem can be added later by passing other labels.

    Returns ``{"ready": False, "reason": …}`` until there are MIN_DAYS labelled
    days covering MIN_CLASSES classes. Otherwise ``ready``, ``prediction``,
    ``confidence`` (share of trees that agree), ``accuracy`` (leave-one-day-out),
    ``hours``, ``days``, ``classes`` (hours per class) and ``importance``
    (feature → share of the forest's decisions). With a ``baseline`` (another
    guess per hour, such as the age schedule) also ``baseline_accuracy``, scored
    on the same held-out hours as ``accuracy``, and ``baseline_now``.
    """
    if hourly.empty:
        return {"ready": False, "reason": "No plant measurements yet: start the timelapse."}
    area = area_column(hourly)
    candidates = (FEATURES if area == "area_cm2" else FRAME_FEATURES) + UPTAKE_FEATURES
    present = [f for f in candidates if f in hourly and hourly[f].notna().any()]
    features = hourly[present].dropna(subset=[area])
    labels = labels.reindex(features.index)
    labelled = labels.notna().to_numpy()
    days = pd.Index(features.index[labelled].date).nunique()
    classes = labels[labelled].value_counts()
    if days < MIN_DAYS or len(classes) < MIN_CLASSES:
        return {
            "ready": False,
            "reason": (f"Needs at least {MIN_DAYS} days of photos covering {MIN_CLASSES} "
                       f"different {what}s; so far {days} day(s) and {len(classes)} "
                       f"({', '.join(classes.index) or 'none'})."),
        }
    train, train_labels = features[labelled], labels[labelled]
    train = train.dropna(axis=1, how="all")
    model, fill = _fit(train, train_labels)
    probabilities = model.predict_proba(features[train.columns].tail(1).fillna(fill))[0]
    best = int(np.argmax(probabilities))
    guesses = held_out_guesses(train, train_labels)
    result = {
        "ready": True,
        "prediction": str(model.classes_[best]),
        "confidence": float(probabilities[best]),
        "accuracy": _agreement(guesses, train_labels),
        "hours": int(labelled.sum()),
        "days": int(days),
        "classes": {str(k): int(v) for k, v in classes.items()},
        "importance": dict(sorted(zip(train.columns, map(float, model.feature_importances_)),
                                  key=lambda item: -item[1])),
    }
    if baseline is not None:
        baseline = baseline.reindex(features.index)
        result["baseline_accuracy"] = _agreement(baseline.reindex(guesses.index), train_labels)
        now = baseline.iloc[-1] if len(baseline) else None
        result["baseline_now"] = None if pd.isna(now) else str(now)
    return result


def estimate_stage(hourly: pd.DataFrame, changes: list[tuple[str, str]],
                   planted_at: datetime | None = None) -> dict:
    """The random forest for growth stage: labels come from the stage the
    controller was on at each hour. With ``planted_at``, the age schedule is
    scored on the same held-out hours as the baseline to beat."""
    features = hourly.dropna(subset=[area_column(hourly)]) if not hourly.empty else hourly
    baseline = age_schedule(features.index, planted_at) if planted_at is not None else None
    return train_classifier(hourly, stage_at(features.index, changes), what="stage",
                            baseline=baseline)
