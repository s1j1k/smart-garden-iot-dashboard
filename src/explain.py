"""Info buttons for the public demo site (smartgarden.tech).

Each section heading gets a small light-blue (i). Clicking it opens a light-blue
dialog that explains, for a general reader, why the section is there. The admin
site shows the plain headings.
"""

from __future__ import annotations

import streamlit as st

from admin_auth import is_admin_site

INFO_BLUE = "#2f86c9"
DIALOG_BLUE = "#e4f2fc"

# key: (dialog title, body in Markdown)
INFO = {
    "home": ("About this project", """
This is a **student-built smart hydroponic garden**: basil and lettuce grow with
their roots in a tank of nutrient water instead of soil (*deep water culture*).
An air stone bubbles the water so the roots get oxygen and the tank stays mixed.

A small **ESP32 microcontroller** reads the sensors every 10 seconds and runs four
pumps (fresh water, nutrient, pH-up and pH-down) to keep the water in the range the
plants like. It makes every decision itself, so the garden keeps working if the
Internet drops. A **Raspberry Pi** stores the readings, takes the photos and serves
this dashboard.

The project is a year-long engineering capstone. This public page is read-only:
it shows what the garden is doing right now.
"""),
    "readings": ("Live readings", """
These tiles are the latest measurements from the tank and the air around it.

- **pH**: how acidic the water is. Roots absorb nutrients best in a narrow
  band (about 5.5 to 6.5 for these plants).
- **Nutrient strength (TDS)**: how much fertiliser is dissolved, in parts per
  million. Too little starves the plants; too much burns the roots.
- **Water temperature and level**: warm water holds less oxygen, and the roots
  need the right gap between the pots and the water surface.
- **Air temperature, humidity, CO₂ and light**: the growing climate.

The coloured target under each tile comes from the plants' current growth stage.
"""),
    "grow_light": ("Grow light", """
Indoors, the plants get their light from an LED strip tuned to the red and blue
wavelengths that photosynthesis uses most (it looks purple, sometimes called
*blurple*).

The light follows a **16-hour day and 8-hour night**, a common schedule for leafy
greens: long enough for strong growth, with a dark period the plants still need.
The controller switches it on and off itself from the time of day.
"""),
    "photo": ("Latest photo", """
A Raspberry Pi camera photographs the plants **every 5 minutes** while the grow
light is on, and every 30 minutes at night, when each photo needs a brief flash. The
photos make a timelapse of the whole grow, and software measures **green coverage**,
the share of the picture that is leaf, as a simple, automatic measure of growth.
"""),
    "plant_health": ("Plant health", """
Software finds each plant in the photos and measures its **leaf area, height, shape
and leaf colour**. It compares every plant with **itself** over the last days rather
than with a fixed number, so it works for basil and lettuce alike:

- **Leggy**: the plant grows taller faster than it grows leaf, a sign of too little
  light.
- **Curling or drooping**: less leaf shows in the photo while the plant keeps its
  height.
- **Paler**: leaves lose green, compared only between photos under the same light.
- **Stalled**: leaf area has hardly grown in three days.

It then checks the live readings (pH, TDS, temperatures, humidity, the state of the
nutrient solution) for conditions known to cause that symptom.
"""),
    "bottles": ("Dosing bottles", """
The controller adds liquids from four bottles with small calibrated pumps: fresh
water, nutrient concentrate, pH-up and pH-down. Because each pump has been measured,
the controller knows how much it has used and warns before a bottle runs empty.
"""),
    "trends": ("Why look at trends?", """
A single reading says how the tank is now; the history shows how it **behaves**.
These charts show each measurement over time, with the target range shaded, and mark
every time the controller added something.

They are how the control system was tested and tuned: after each dose you can see how
long the water takes to mix and how far the reading moves.
"""),
    "trend_ph": ("pH over time", """
pH measures acidity on a scale from 0 (very acidic) to 14 (very alkaline). Plants take
up nutrients best in slightly acidic water, about **5.5 to 6.5** here.

pH drifts on its own as plants feed. When it leaves the range, the controller adds a
small dose of pH-up or pH-down, **waits about 20 minutes for the tank to mix**, and
only then measures again before deciding on another dose. The dashed lines are those
doses: ▲ pH-up, ▼ pH-down.
"""),
    "trend_tds": ("Nutrient strength over time", """
TDS (*total dissolved solids*) estimates how much fertiliser is in the water from how
well it conducts electricity, in parts per million (ppm).

When it falls below the range, the controller adds nutrient concentrate in a size
calculated from how far it has to go, then waits for it to mix. It never adds nutrient
when the reading is high: the only fix for too strong a solution is fresh water.
"""),
    "trend_level": ("Water level over time", """
The plants drink and the water evaporates, so the level slowly falls. The roots like a
small air gap between the pots and the water, which also gives them oxygen. When the
level drops too far, the top-up pump adds fresh water (shaded in green).
"""),
    "trend_temp": ("Temperature over time", """
**Air** temperature affects how fast the plants grow and transpire. **Water**
temperature matters for the roots: warm water holds less dissolved oxygen.

The water temperature is also used to correct the pH and nutrient readings, which both
change slightly with temperature.
"""),
    "trend_humidity": ("Humidity over time", """
Relative humidity is how much moisture the air holds compared with the most it could
hold at that temperature. Very dry air makes leaves lose water quickly; very humid air
encourages mould. Leafy greens are comfortable in the middle of the range.
"""),
    "trend_co2": ("CO₂ over time", """
Plants build their leaves from carbon dioxide taken from the air. Indoors, CO₂ rises
when people are in the room and falls when it is ventilated. Outdoor air is about
420 ppm; a closed room can reach well over 1000.
"""),
    "trend_light": ("Light sensor over time", """
A simple light sensor checks that the grow light really is on when it should be. It
reads relative brightness and is uncalibrated, so it is only used to check that the
light is on.
"""),
    "stage": ("Grow stages", """
Plants need different conditions as they grow. Seedlings want gentle, weak nutrient
water; mature leafy plants want stronger nutrient and a bigger air gap for their roots.

The garden steps through **seedling, transitional, vegetative and harvest** stages,
and each one sets its own pH, nutrient and water-level targets for the controller.
"""),
    "stage_ready": ("When to change stage", """
The plants' age suggests a stage, but plants grow at different speeds. This checklist
lists the signs to look for, such as the number of true leaves or the roots reaching
the water, so a person decides the switch from the plants themselves.
"""),
    "stage_timeline": ("Predicted vs actual stage", """
The dashed line is when each stage is due, worked out from the conditions the plants
have had: every day counts by how good its temperature and light were for the slower
crop, so a cold or dark spell moves every later stage back. The solid line is the stage
the controller was really set to. The gap shows whether the switches were early or late.
"""),
    "camera": ("Camera and timelapse", """
A Raspberry Pi Camera Module 3 above the tank takes a photo every 5 minutes. Together
the photos form a timelapse, and simple image processing turns each one into growth
numbers, so plant growth can be measured automatically rather than only by eye.
"""),
    "camera_status": ("Latest frame and growth metrics", """
**Green coverage** is the percentage of the picture made up of leaf-green pixels.
**Plant area** is the size of that green region in pixels. Both are simple first
measures of growth; they will be calibrated once the camera position is final.
"""),
    "camera_plants": ("Each plant", """
The camera finds each plant in the photo and draws a numbered box around it. The two
70 mm net-pot rims are the ruler, so sizes are in centimetres and do not change when the
camera moves. Photos taken without the white flash are not measured. For each plant it
measures **leaf area**, **height** and **width** (the size of its box), **leaves seen** (leaf blades, with touching leaves
split where they narrow; leaves hidden behind others are missed) and **growth** (how
much its leaf area changed over the last day).
"""),
    "camera_stage": ("Random forest: growth stage", """
A step towards the garden recognising the growth stage by itself. Every hour of
plant measurements is labelled with the stage the controller was actually on, and a
**random forest** (many small decision trees that vote) learns which measurements go
with which stage, then guesses the stage of the latest hour. Besides the photos it
uses how much **water** and **nutrient** the plants took up over the last day, worked out
from the tank's level and TDS with the controller's own additions taken out. It does
not see the plants' age, pH or TDS level, because the controller sets those from the
stage and the forest would just read the answer back.

**Accuracy** is how often it is right on a whole day it never saw while learning. The
**age schedule** is scored on the same hours: it is the stage the plants' age alone
gives, and the forest is only useful where it beats it. It is only a suggestion: the
stage still changes only when you apply it on the Grow stage page.
"""),
    "camera_harvest": ("Random forest: harvest date", """
Two independent estimates of when each plant will be ready to harvest.

**From canopy:** the camera measures each plant's leaf area in square centimetres every
few minutes. A **random forest** learns how fast the leaf area grows each day under the
conditions that day had (temperature, grow-light hours, nutrient and pH in range) and
the plant's size. It then grows today's plant forward, day by day, under the conditions
of the last few days until it reaches harvest size. The range comes from how much the
trees disagree.

**From conditions:** the development model on the Grow stage page, which counts each
day by how good it was for that crop.

Both are estimates: the harvest size is a guess until the first harvest is measured.
"""),
    "stage_forest": ("Random forest predictions", """
Two **random forests** (many small decision trees that vote) learn from this garden's own
records.

**Growth stage:** learns which camera measurements (leaf area, height, width, leaf count,
growth rate) and which water and nutrient use go with each stage the controller has run,
then names the stage of the latest hour. It needs photos from at least two stages before it
can learn anything, and it is scored on whole days it never saw, against plant age alone.

**Harvest date:** learns how fast each plant's leaf area grows under each day's
temperature, light and nutrient conditions, then grows today's plant forward until it
reaches harvest size. The range comes from how much the trees disagree.

Both are suggestions: the stage changes only when you apply it.
"""),
    "stage_why": ("Why the stages move", """
The age schedule assumes every day is a perfect growing day. Real days are not: the
grow light may have been off or short, and the room may have been too cold or too warm
for that crop (lettuce likes it cooler than basil). Each day is counted by how much it
was worth, between 0 and 1 of a perfect day, and each stage starts once enough
perfect-day equivalents have built up.

Days with no readings, or when the plants were out of the tank while the garden was
being rebuilt, are not guessed: they are counted at the plants' typical rate for the
central date, and at 0 or 1 for the latest and earliest dates.
"""),
    "camera_history": ("Growth history", """
These curves show green coverage and plant area over time. A steadily rising curve
means healthy growth; a flattening curve late in the vegetative stage is one sign the
plants are ready to harvest.
"""),
    "camera_table": ("Recent measurements", """
The numbers behind the charts: one row per photo, with its time, green coverage, plant
area and whether a sprout was detected. They are stored with the sensor data, so
growth can later be compared with the water and climate conditions.
"""),
    "alerts": ("Alerts", """
The garden checks itself continuously. **Faults** mean something is broken or a
reading cannot be trusted; **warnings** mean the garden needs attention, such as an
empty bottle; **reminders** are gentle nudges, such as changing the grow stage.

An alert is only pushed as a notification once it has lasted long enough to matter,
so brief blips do not cause false alarms.
"""),
    "notifications": ("Notifications", """
While this dashboard is open in a browser, or installed as an app, it can show a
system notification for each new alert. The garden's owners also receive alerts on
their phones even when no dashboard is open.
"""),
    "learn": ("Learn", """
This page explains the measurements in more depth: what each one means for the plants,
the range the garden aims for, and whether it is controlled automatically or only
monitored.
"""),
}

CSS = f"""
<style>
[class*="st-key-info_"] button {{
  color: {INFO_BLUE} !important; padding: 0 0.15rem; min-height: 0;
}}
[class*="st-key-info_"] button:hover {{ background: {DIALOG_BLUE}; }}
[data-testid="stDialog"] div:has(> [role="dialog"]) {{
  background: {DIALOG_BLUE} !important; border: 1px solid {INFO_BLUE};
}}
[data-testid="stDialog"] [role="dialog"] * {{ color: #111 !important; }}
</style>
"""


def enabled() -> bool:
    """Info buttons appear on the public site only."""
    return not is_admin_site()


def style() -> None:
    """Inject the light-blue button and dialog styling once per page run."""
    if enabled():
        st.html(CSS)


def _open(key: str) -> None:
    title, body = INFO[key]
    st.dialog(title, width="medium")(lambda: st.markdown(body))()


def info_button(key: str) -> None:
    if not enabled():
        return
    if st.button(":material/info:", key=f"info_{key}", type="tertiary",
                 help=f"What is this? {INFO[key][0]}"):
        _open(key)


def heading(text: str, key: str, kind: str = "markdown") -> None:
    """A section heading with its info button beside it on the public site.

    ``kind`` is "title", "subheader" or "markdown" (``text`` then carries its own
    ``**bold**`` or ``####``).
    """
    write = {"title": st.title, "subheader": st.subheader, "markdown": st.markdown}[kind]
    if not enabled():
        write(text)
        return
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        write(text, width="content")
        info_button(key)
