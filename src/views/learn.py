"""Learn: why each measurement matters for growing healthy basil and lettuce."""

from __future__ import annotations

import streamlit as st

import explain
TOPICS = [
    ("🧪", "pH — can the roots take up food?", "Controlled · set by your plants", """
pH decides which nutrients the roots can actually absorb. In water culture
leafy greens grow well anywhere in about **5.5–6.5**. The garden aims for the
middle of your plants' shared optimum (basil + lettuce: **6.0**) and only
doses when pH leaves the range they tolerate (basil + lettuce: **5.5–6.5**).

- **Why not hold it exactly?** Plants move pH themselves as they take up
  nutrients — a slow rise is normal and healthy. Correcting every small swing
  wastes reagent, builds up salts and stresses roots.
- **Too high (above ~6.5):** iron and manganese lock out. New leaves turn
  yellow between green veins even though the food is in the water.
- **Too low (below ~5.5):** calcium and magnesium uptake falls and roots get
  damaged — brown tips and stunted growth.
- The nutrient concentrate itself shifts pH, so the controller always doses
  **nutrient first, waits for it to mix, then corrects pH**.
- pH probes drift. An uncalibrated probe can be several tenths out, which is
  why calibration warnings matter.
"""),
    ("🌿", "TDS / EC — how strong is the feed?", "Controlled · stage-dependent", """
TDS (total dissolved solids, in ppm) measures how much fertiliser salt is in the
water — the plants' food strength. (EC is the same idea in mS/cm; 1.0 mS/cm ≈ 500 ppm.)

- **Seedlings** get **no nutrient** — the plug has enough and tender roots burn easily.
- **Transitional** feeds at **half strength** while roots establish.
- **Vegetative** feeds at the strength your plants share (basil + lettuce:
  **600–800 ppm**) for fast leaf growth.
- **Too weak:** pale, slow, small leaves. **Too strong:** leaf-tip burn and
  wilting, because salty water pulls moisture *out* of the roots.
- Nutrient goes in as **small, capped doses**, each followed by a **20-minute
  mixing wait** before the reading is trusted again. The total added since the
  tank was last fresh water is limited, so the feed cannot overshoot.
- Plants drink water faster than they eat salts, so TDS slowly **creeps up**.
  The controller only adds nutrient — when TDS is high, top up with plain water.
"""),
    ("💡", "Grow light — energy for growth", "Controlled · 16 h on / 8 h off", """
Light is the plants' only energy source. What counts is the **daily light
integral** — brightness × hours. The strip is modest, so it runs **16 hours a day**.

- **Blue** light keeps basil and lettuce compact and leafy; **red** drives
  photosynthesis. Their mix gives the blurple glow.
- The **8-hour dark period is part of the recipe**: plants use the night to
  move sugars and repair. Never run it 24 h — and long, hot days push lettuce
  to bolt (flower and turn bitter).
- The Home page timeline shows when the light is meant to be on and when it
  comes back on.
"""),
    ("💧", "Water level — water *and* air for the roots", "Controlled · drawdown by stage", """
Roots need oxygen as much as water.

- **Seedlings:** water is held right up to the net pots so short roots can reach it.
- **Later stages:** the level is lowered **20 mm** (30 mm at harvest). Roots grow
  down to chase the water, and the exposed part breathes in the humid air gap —
  this is what keeps deep-water roots white and healthy.
- A very low reservoir dries roots fast; the top-up pump refills from the water jug.
"""),
    ("🌡️", "Water temperature — oxygen in the water", "Monitored · 18–24 °C", """
Warm water holds **less dissolved oxygen**.

- **Above ~24 °C:** roots suffocate and *Pythium* root rot (brown, slimy roots)
  thrives. Shade the reservoir and keep it off warm floors.
- **Below ~16 °C:** nutrient uptake and growth slow right down.
"""),
    ("🌤️", "Air temperature — growth speed", "Monitored · about 18–27 °C", """
- **Basil** loves warmth (20–28 °C) and sulks below 15 °C.
- **Lettuce** prefers it cooler (16–24 °C) and **bolts** when it is hot.
- The ranges here are the overlap that suits both. Remember the light adds heat.
"""),
    ("💦", "Humidity — how fast leaves lose water", "Monitored · stage-dependent", """
- **Seedlings** like it humid (**60–80 %**) — they cannot yet replace water fast.
  A clear dome helps.
- **Bigger plants** prefer **45–70 %**. Constantly damp, still air invites
  mildew and mould, and stops calcium moving to new leaves (tip burn).
- Very dry air makes leaves wilt even when the roots are wet.
"""),
    ("💨", "CO₂ — the raw material", "Monitored · 400–1500 ppm", """
Plants build their leaves out of CO₂ from the air.

- Outdoor air is about **420 ppm**. A closed room with plants can drop below that
  during the day and slow growth; open a window.
- **Up to ~1200 ppm** speeds growth; a stuffy room with people often sits at
  800–1500 ppm, which is fine for plants.
- Readings **below 400 ppm** indoors usually mean the sensor needs resetting.
"""),
    ("☀️", "Light sensor — is the light really on?", "Monitored", """
The light sensor (LDR) confirms the grow light is actually lighting the
plants and shows daylight from the window. If the light is scheduled on but the
sensor stays dark, check the strip and its power supply.
"""),
    ("📷", "Camera — watching the plants grow", "Monitored · photo every 5 min", """
The 5-minute photo measures **green coverage** — how much of the frame is leaf.
A steadily rising curve means healthy growth; a plateau late in vegetative
growth is one sign it is time to harvest. Use the photo with the checklist on
the **Grow stage** page to decide when to switch stages.
"""),
]


def page():
    explain.heading("📚 Why these numbers matter", "learn", "title")
    st.caption("A smart garden keeps a few things in the right range so the plants spend "
               "their energy growing leaves rather than coping with stress. Controlled items "
               "are adjusted automatically; monitored ones raise an alert when they drift.")
    for i in range(0, len(TOPICS), 2):
        cols = st.columns(2)
        for col, (icon, title, badge, body) in zip(cols, TOPICS[i:i + 2]):
            with col, st.container(border=True):
                st.markdown(f"### {icon} {title}")
                st.caption(badge)
                st.markdown(body)
