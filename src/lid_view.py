"""Top-down view of the reservoir lid: click a net-pot hole to choose its plant.

The outline, holes and sensor ports follow the laser-cut lid drawing
(Hydroponics Lid Drawing, A1 at 1:1), in millimetres, measured off the
drawing, so positions are close but not exact dimensions. Pot 1 is the left
hole and pot 2 the right, the same numbering the camera page uses for its
per-pot boxes.

Rendered with ``st.components.v2`` (inline SVG + JavaScript, no build step):
the component sends ``{"pot": index, "plant": id}`` back when a plant is picked.
"""

from __future__ import annotations

import json

import streamlit as st

import garden_settings

# Lid geometry in mm, from the drawing (origin top-left of the lid).
LID_W, LID_H, LID_R = 425, 322, 23
POT_HOLES = [(105, 161, 29), (314, 161, 29)]          # (x, y, radius): left, right
PORTS = [  # (label, x, y, radius or None for the slot, what it is)
    ("pH", 66, 292, 15.6, "pH probe"),
    ("T", 138, 292, 8, "Water temperature probe (DS18B20)"),
    ("WL", 205, 292, None, "Water-level sensor slot"),
    ("EC", 274, 292, 8, "EC / TDS probe"),
    ("P", 354, 292, 14, "Pump lines: water, nutrient, pH up, pH down and the air pump"),
]
MOUNTING_HOLES = [(7, 39), (417, 39), (7, 283), (417, 283)]
EMPTY = ""

CSS = """
.lid-wrap { position: relative; width: 100%; max-width: 640px; margin: 0 auto; font-family: inherit; }
.lid-wrap svg { width: 100%; height: auto; display: block; }
.lid-plate { fill: #1d1f27; stroke: #6b7080; stroke-width: 1.2; }
.lid-gloss { fill: url(#lidGloss); pointer-events: none; }
.port { fill: #0e0f14; stroke: #8a8f9c; stroke-width: 0.8; }
.port-label { fill: #c9ccd6; font-size: 11px; font-weight: 600; text-anchor: middle; }
.mount { fill: #0e0f14; stroke: #8a8f9c; stroke-width: 0.6; }
.pot { cursor: default; }
.pot.editable { cursor: pointer; }
.pot-ring { fill: #127a86; stroke: #0b5560; stroke-width: 2; transition: all 0.15s ease; }
.pot-mesh { fill: #0a3d45; }
.pot-mesh-line { stroke: #127a86; stroke-width: 0.8; opacity: 0.6; }
.pot.editable:hover .pot-ring, .pot.selected .pot-ring { fill: #19a2b1; stroke: #f5d76e; stroke-width: 3; }
.pot-emoji { font-size: 30px; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
.pot-name { fill: #f2f3f7; font-size: 12px; font-weight: 600; text-anchor: middle; pointer-events: none; }
.pot-number { fill: #9fe3ea; font-size: 10px; text-anchor: middle; pointer-events: none; }
.tip { position: absolute; pointer-events: none; background: rgba(20, 22, 30, 0.92); color: #fff;
       padding: 4px 8px; border-radius: 6px; font-size: 12px; white-space: nowrap;
       transform: translate(-50%, -130%); opacity: 0; transition: opacity 0.1s; }
.menu { position: absolute; background: var(--st-background-color, #fff);
        color: var(--st-text-color, #222); border: 1px solid rgba(128, 128, 128, 0.35);
        border-radius: 10px; box-shadow: 0 6px 22px rgba(0, 0, 0, 0.25); padding: 6px;
        min-width: 170px; transform: translate(-50%, 8px); z-index: 10; display: none; }
.menu-title { font-size: 12px; opacity: 0.7; padding: 2px 8px 6px; }
.menu button { display: block; width: 100%; text-align: left; border: 0; background: none;
               color: inherit; font: inherit; font-size: 14px; padding: 6px 8px; border-radius: 6px;
               cursor: pointer; }
.menu button:hover { background: rgba(25, 162, 177, 0.15); }
.menu button.current { font-weight: 700; }
"""

JS = """
export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  const NS = "http://www.w3.org/2000/svg";
  parentElement.querySelectorAll(".lid-wrap").forEach((el) => el.remove());
  const wrap = document.createElement("div");
  wrap.className = "lid-wrap";
  parentElement.appendChild(wrap);
  const g = data.geometry;
  const pad = 14;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `${-pad} ${-pad} ${g.w + 2 * pad} ${g.h + 2 * pad}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Top-down view of the reservoir lid");
  wrap.appendChild(svg);
  const el = (name, attrs, parent = svg) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    parent.appendChild(node);
    return node;
  };
  const defs = el("defs", {});
  const grad = el("linearGradient", { id: "lidGloss", x1: "0", y1: "0", x2: "1", y2: "1" }, defs);
  el("stop", { offset: "0", "stop-color": "#ffffff", "stop-opacity": "0.10" }, grad);
  el("stop", { offset: "0.45", "stop-color": "#ffffff", "stop-opacity": "0" }, grad);
  el("stop", { offset: "1", "stop-color": "#ffffff", "stop-opacity": "0.04" }, grad);

  el("rect", { class: "lid-plate", x: 0, y: 0, width: g.w, height: g.h, rx: g.r, ry: g.r });
  el("rect", { class: "lid-gloss", x: 0, y: 0, width: g.w, height: g.h, rx: g.r, ry: g.r });
  for (const [x, y] of g.mounts) el("circle", { class: "mount", cx: x, cy: y, r: 2.2 });

  const tip = document.createElement("div");
  tip.className = "tip";
  wrap.appendChild(tip);
  const showTip = (text, x, y) => {
    const box = svg.getBoundingClientRect(), outer = wrap.getBoundingClientRect();
    const sx = box.width / (g.w + 2 * pad), sy = box.height / (g.h + 2 * pad);
    tip.textContent = text;
    tip.style.left = `${box.left - outer.left + (x + pad) * sx}px`;
    tip.style.top = `${box.top - outer.top + (y + pad) * sy}px`;
    tip.style.opacity = 1;
  };
  const hideTip = () => { tip.style.opacity = 0; };

  for (const port of g.ports) {
    const group = el("g", {});
    if (port.r) el("circle", { class: "port", cx: port.x, cy: port.y, r: port.r }, group);
    else el("rect", { class: "port", x: port.x - 10.5, y: port.y - 2.5, width: 21, height: 5 }, group);
    const label = el("text", { class: "port-label", x: port.x, y: port.y - (port.r || 6) - 7 }, group);
    label.textContent = port.label;
    group.addEventListener("mouseenter", () => showTip(port.what, port.x, port.y - (port.r || 6)));
    group.addEventListener("mouseleave", hideTip);
  }

  const menu = document.createElement("div");
  menu.className = "menu";
  wrap.appendChild(menu);
  const closeMenu = () => { menu.style.display = "none"; svg.querySelectorAll(".pot").forEach((p) => p.classList.remove("selected")); };

  data.pots.forEach((pot, index) => {
    const [x, y, r] = g.holes[index];
    const group = el("g", { class: "pot" + (data.editable ? " editable" : ""), tabindex: data.editable ? 0 : -1 });
    el("circle", { class: "pot-ring", cx: x, cy: y, r: r + 9 }, group);
    el("circle", { class: "pot-mesh", cx: x, cy: y, r: r }, group);
    for (let k = -2; k <= 2; k++) {
      const off = k * r / 2.6, half = Math.sqrt(Math.max(r * r - off * off, 0));
      el("line", { class: "pot-mesh-line", x1: x + off, y1: y - half, x2: x + off, y2: y + half }, group);
      el("line", { class: "pot-mesh-line", x1: x - half, y1: y + off, x2: x + half, y2: y + off }, group);
    }
    const emoji = el("text", { class: "pot-emoji", x: x, y: y }, group);
    emoji.textContent = pot.emoji || "·";
    const name = el("text", { class: "pot-name", x: x, y: y + r + 24 }, group);
    name.textContent = pot.label || "Empty";
    const number = el("text", { class: "pot-number", x: x, y: y - r - 14 }, group);
    number.textContent = `Pot ${index + 1}`;
    const hint = data.editable ? " · click to change" : "";
    group.addEventListener("mouseenter", () => showTip(`Pot ${index + 1}: ${pot.label || "empty"}${hint}`, x, y - r - 9));
    group.addEventListener("mouseleave", hideTip);
    if (!data.editable) return;
    const open = (event) => {
      event.stopPropagation();
      hideTip();
      closeMenu();
      group.classList.add("selected");
      menu.innerHTML = "";
      const title = document.createElement("div");
      title.className = "menu-title";
      title.textContent = `What is in pot ${index + 1}?`;
      menu.appendChild(title);
      for (const option of data.options) {
        const button = document.createElement("button");
        button.textContent = `${option.emoji} ${option.label}`;
        if (option.id === pot.id) button.className = "current";
        button.addEventListener("click", (e) => {
          e.stopPropagation();
          closeMenu();
          setTriggerValue("pick", { pot: index, plant: option.id });
        });
        menu.appendChild(button);
      }
      const box = svg.getBoundingClientRect(), outer = wrap.getBoundingClientRect();
      const sx = box.width / (g.w + 2 * pad), sy = box.height / (g.h + 2 * pad);
      menu.style.left = `${box.left - outer.left + (x + pad) * sx}px`;
      menu.style.top = `${box.top - outer.top + (y + r + pad) * sy}px`;
      menu.style.display = "block";
    };
    group.addEventListener("click", open);
    group.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") open(e); });
  });
  wrap.addEventListener("click", closeMenu);
}
"""

_lid = st.components.v2.component("hydroponic_lid", css=CSS, js=JS)


def pot_plants(settings: dict | None = None) -> list[str]:
    """Plant id in each pot, left to right ("" = empty)."""
    settings = settings or garden_settings.load()
    pots = list(settings.get("pots") or settings.get("plants") or [])
    return (pots + [EMPTY] * len(POT_HOLES))[:len(POT_HOLES)]


def set_pot(index: int, plant_id: str) -> list[str]:
    """Put ``plant_id`` in pot ``index`` and keep the reservoir's plant list in step."""
    pots = pot_plants()
    pots[index] = plant_id
    plants = list(dict.fromkeys(p for p in pots if p))
    garden_settings.save({"pots": pots, "plants": plants})
    return pots


def render_lid(catalogue, emoji_for, editable: bool) -> list[str]:
    """Draw the lid; with ``editable`` a click on a pot picks its plant.

    ``catalogue`` is the plant list (``id``, ``common_name``) and ``emoji_for``
    maps an id to its emoji. Returns the plant id in each pot.
    """
    names = {p.id: p.common_name for p in catalogue}
    pots = pot_plants()
    data = {
        "editable": editable,
        "geometry": {
            "w": LID_W, "h": LID_H, "r": LID_R,
            "holes": POT_HOLES,
            "mounts": MOUNTING_HOLES,
            "ports": [{"label": label, "x": x, "y": y, "r": r, "what": what}
                      for label, x, y, r, what in PORTS],
        },
        "pots": [{"id": pid, "label": names.get(pid, pid.replace("_", " ").title()) if pid else "",
                  "emoji": emoji_for(pid) if pid else ""} for pid in pots],
        "options": [{"id": p.id, "label": p.common_name, "emoji": emoji_for(p.id)}
                    for p in catalogue] + [{"id": EMPTY, "label": "Empty", "emoji": "⬜"}],
    }
    result = _lid(data=json.loads(json.dumps(data)), key="hydroponic_lid",
                  on_pick_change=lambda: None)
    pick = getattr(result, "pick", None)
    if editable and isinstance(pick, dict) and 0 <= int(pick.get("pot", -1)) < len(POT_HOLES):
        pots = set_pot(int(pick["pot"]), str(pick.get("plant") or EMPTY))
        st.rerun()
    return pots
