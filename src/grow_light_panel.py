"""Grow light photoperiod card: is the light meant to be on, and when it next switches.

The ESP32 runs one ON window and one OFF window per day (firmware
``shared/grow_light.h``). Telemetry carries:

    grow_light_on                  light is lit now (includes boot animation / fades)
    grow_light_mode                on | off | fade_in | fade_out | animating
    grow_light_photoperiod_h       hours ON per day (16)
    grow_light_schedule_start_utc  UTC hour the ON window starts (22 = 08:00 AEST)
    grow_light_ntp_schedule        true: follows the clock; false: counted from boot
    grow_light_want_on             schedule says ON right now          (firmware 1.6.3+)
    grow_light_schedule_off_utc    UTC hour the ON window ends         (firmware 1.6.3+)
    grow_light_next_change_s       seconds to the next ON <-> OFF switch (firmware 1.6.3+)
    grow_light_makeup              lit after the scheduled OFF to repay missed light (1.9.23+)
    grow_light_makeup_armed        make-up started by an operator; runs until nothing is owed
    grow_light_makeup_owed_s       scheduled light missed while the garden was off
    grow_light_makeup_max_night_s  most make-up light per night (keeps the dark period)
    grow_light_manual              switched on by hand (LIGHT ON / light_on), whatever the schedule
    grow_light_manual_left_s       seconds the manual on still runs, then back to the schedule

Everything is placed on one "anchor": a moment an ON window started. The
schedule repeats every 24 h from it, so the card can say where "now" is even
between messages, and draws today's day in the dashboard's local time.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from status_panel import _parse_time, fmt_duration

DAY = timedelta(hours=24)


def _anchor(gl, received_at):
    """A datetime when an ON window started, or None if the schedule cannot be placed."""
    on_hours = gl.get("on_hours")
    if on_hours is None:
        return None
    sent = _parse_time(received_at)
    next_change_s = gl.get("next_change_s")
    if sent is not None and next_change_s is not None and next_change_s >= 0 \
            and gl.get("want_on") is not None:
        change = sent + timedelta(seconds=float(next_change_s))
        return change - timedelta(hours=float(on_hours)) if gl["want_on"] else change
    # Older firmware: only the clock schedule can be placed (from its UTC start hour).
    if gl.get("clock") and gl.get("start_utc") is not None:
        ref = sent or datetime.now(timezone.utc)
        return ref.replace(hour=int(gl["start_utc"]) % 24, minute=0, second=0, microsecond=0)
    return None


def photoperiod(gl, received_at, now=None):
    """Where the schedule is now: want_on, next switch time, and today's ON windows."""
    now = now or datetime.now(timezone.utc)
    anchor = _anchor(gl or {}, received_at)
    if anchor is None:
        return None
    on_len = timedelta(hours=float(gl["on_hours"]))
    cycle = (now - anchor) % DAY
    want_on = cycle < on_len
    next_change = now + ((on_len - cycle) if want_on else (DAY - cycle))

    # Today in the dashboard's local time, and the ON windows that overlap it.
    local_now = now.astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + DAY
    base = anchor + ((day_start - anchor) // DAY) * DAY
    windows = []
    for k in (-1, 0, 1):
        start, end = base + k * DAY, base + k * DAY + on_len
        if end > day_start and start < day_end:
            windows.append((max(start, day_start), min(end, day_end)))

    on_start = (anchor + ((now - anchor) // DAY) * DAY).astimezone()
    return {
        "want_on": want_on,
        "next_change": next_change,
        "until_change_s": (next_change - now).total_seconds(),
        "on_hours": float(gl["on_hours"]),
        "on_local": on_start.strftime("%H:%M"),
        "off_local": (on_start + on_len).strftime("%H:%M"),
        "day_start": day_start,
        "windows": windows,
        "now": local_now,
    }


# Telemetry arrives every 10 s. A longer silence means the controller was off
# or rebooting, so the light was not running its schedule.
GAP_S = 120


def last_on_window(gl, received_at, now=None):
    """(start, end, "current" | "last") of the ON window the light is in now (up to
    now), or in the dark period the one that just ended. None if it cannot be placed."""
    now = now or datetime.now(timezone.utc)
    anchor = _anchor(gl or {}, received_at)
    if anchor is None:
        return None
    on_len = timedelta(hours=float(gl["on_hours"]))
    start = anchor + ((now - anchor) // DAY) * DAY
    if now - start < on_len:
        return start, now, "current"
    return start, start + on_len, "last"


def missed_light_s(times, start, end, gap_s=GAP_S):
    """Seconds of [start, end) with no telemetry gap shorter than gap_s around them:
    the time the controller was off, so the plants missed that light. ``times`` are
    the message times (UTC datetimes); include a few from before ``start``."""
    pts = sorted(t for t in times if t is not None)
    covered = 0.0
    for a, b in zip(pts, pts[1:]):
        if (b - a).total_seconds() <= gap_s:
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                covered += (hi - lo).total_seconds()
    if pts:
        # The last message stands for the time up to the next one due.
        lo, hi = max(pts[-1], start), min(pts[-1] + timedelta(seconds=gap_s), end)
        if hi > lo:
            covered += (hi - lo).total_seconds()
    return max(0.0, (end - start).total_seconds() - covered)


def render_light_on(state, node_live, on_send, missed=None, send_error=None):
    """Admin: switch the grow light on now for some hours, then back to the schedule.

    ``on_send(hours)`` publishes {"cmd": "light_on", "hours": hours}, or
    {"cmd": "light_auto"} for ``hours=None``. ``missed`` is (seconds, "current" |
    "last") from :func:`missed_light_s`, or None when it cannot be worked out.
    """
    import streamlit as st

    st.markdown("**💡 Grow light on now**")
    if send_error:
        st.error(send_error)
    gl = state.get("grow_light") or {}
    if gl.get("manual") is None:
        st.caption("This firmware cannot switch the light on outside its schedule "
                   "(needs LIGHT ON, firmware 1.9.23 from 16 Oct).")
        return
    if gl.get("manual"):
        st.info(f"On by hand: {fmt_duration(gl.get('manual_left_s'))} left, then back to "
                "the schedule.")
        st.button("Back to the schedule now", key="light_auto", disabled=not node_live,
                  on_click=on_send, args=(None,),
                  help='Sends {"cmd": "light_auto"}.')
        return
    default_h = 1.0
    if missed is not None:
        missed_s, which = missed
        if missed_s >= 360:
            st.caption(f"The controller was off for **{fmt_duration(missed_s)}** of the "
                       f"{which} light period (gaps in its telemetry), so the plants missed "
                       "that much light.")
            default_h = min(16.0, round(missed_s / 3600, 1))
        else:
            st.caption(f"No light missed in the {which} light period.")
    col_h, col_btn = st.columns([1, 2], vertical_alignment="bottom")
    with col_h:
        hours = st.number_input("Hours", min_value=0.1, max_value=16.0, value=default_h,
                                step=0.5, key="light_on_hours")
    with col_btn:
        st.button(f"Switch on for {hours:g} h", key="light_on", type="primary",
                  disabled=not node_live, on_click=on_send, args=(hours,),
                  help='Sends {"cmd": "light_on", "hours": h}: on now, whatever the '
                  "schedule, then back to the schedule. At most 16 h.")


def _hhmm(dt):
    return dt.astimezone().strftime("%H:%M")


def headline(gl, sched, node_live):
    """(kind, title, detail). kind: on / off / warn / unknown."""
    if not gl:
        return "unknown", "Grow light not reported", "Firmware did not send grow-light fields."
    mode = gl.get("mode") or ""
    lit = gl.get("on")
    if sched is None:
        state = "ON" if lit else "OFF"
        source = "the clock" if gl.get("clock") else "time since boot (no NTP)"
        return ("on" if lit else "off", f"Grow light {state}",
                f"{int(gl.get('on_hours') or 16)} h on per day, counted from {source}. "
                "Update the firmware (1.6.3+) to see the switch times.")

    when = f"{_hhmm(sched['next_change'])} (in {fmt_duration(sched['until_change_s'])})"
    if not node_live:
        stale = " Not live — from the last report; the light may have changed since."
    else:
        stale = ""
    if mode == "flash":
        return "on", "Grow light: camera flash", (
            "White light for a photo; it goes back to the schedule in a few seconds.")
    if mode == "animating":
        return "on", "Grow light: boot animation", (
            f"Then follows the schedule — {'ON' if sched['want_on'] else 'OFF'} now.{stale}")
    if mode in ("fade_in", "fade_out"):
        verb = "Fading in" if mode == "fade_in" else "Fading out"
        return "on", f"Grow light: {verb.lower()}", f"{verb} at the scheduled switch.{stale}"
    if gl.get("makeup") and lit and not sched["want_on"]:
        return "on", "Grow light ON — making up missed light", (
            f"{fmt_duration(gl.get('makeup_owed_s'))} still owed. "
            f"Dark from the end of make-up until {when}.{stale}")
    if sched["want_on"] and lit:
        return "on", "Grow light ON — day period", f"Turns off at {when}.{stale}"
    if not sched["want_on"] and not lit:
        return "off", "Grow light OFF — scheduled dark period", (
            f"Back on at {when}.{stale}")
    if node_live:
        expected = "on" if sched["want_on"] else "off"
        return "warn", f"Grow light {'ON' if lit else 'OFF'} — schedule says {expected}", (
            f"Check the LED supply and GPIO 27 data line. Next switch {when}.")
    return "off" if not lit else "on", f"Grow light {'ON' if lit else 'OFF'} (last report)", \
        f"Next scheduled switch {when}.{stale}"


_CSS = """
<style>
.gl-card{border-radius:14px;padding:14px 16px 10px;margin-bottom:1.5rem;
  border:1px solid rgba(140,110,255,.35);
  background:linear-gradient(135deg,rgba(124,92,255,.10),rgba(180,77,255,.05));}
.gl-card.gl-on{border-color:rgba(160,120,255,.8);
  box-shadow:0 0 18px rgba(124,92,255,.45),inset 0 0 24px rgba(180,77,255,.12);}
.gl-card.gl-warn{border-color:#d4a72c;}
.gl-head{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.gl-bulb{flex:none;width:34px;height:34px;border-radius:50%;
  background:radial-gradient(circle at 40% 35%,#3a3358,#211d33);border:2px solid #4b4270}
.gl-on .gl-bulb{background:radial-gradient(circle at 40% 35%,#f2e6ff,#b46bff 45%,#6d4aff);
  border-color:#d8c4ff;animation:gl-pulse 2.6s ease-in-out infinite}
@keyframes gl-pulse{
  0%,100%{box-shadow:0 0 10px 2px rgba(150,100,255,.8),0 0 30px 8px rgba(180,77,255,.45)}
  50%{box-shadow:0 0 16px 5px rgba(170,120,255,.95),0 0 46px 16px rgba(180,77,255,.6)}}
.gl-title{font-weight:700;font-size:1.05rem}
.gl-sub{opacity:.85;font-size:.9rem}
.gl-track{position:relative;height:30px;border-radius:8px;background:rgba(90,80,140,.28);
  overflow:visible;margin:26px 0 2px}
.gl-seg{position:absolute;top:0;bottom:0;border-radius:8px;
  background:linear-gradient(90deg,#6d4aff,#9b5cff 50%,#c04dff);
  box-shadow:0 0 10px rgba(140,90,255,.75),0 0 22px rgba(180,77,255,.35);
  color:#fff;font-size:.75rem;font-weight:600;display:flex;align-items:center;
  justify-content:center;white-space:nowrap;overflow:hidden}
.gl-seg.gl-live{animation:gl-glow 2.6s ease-in-out infinite}
@keyframes gl-glow{0%,100%{filter:brightness(1)}50%{filter:brightness(1.25)}}
.gl-dark{position:absolute;top:0;bottom:0;display:flex;align-items:center;
  justify-content:center;font-size:.75rem;opacity:.75;white-space:nowrap;overflow:hidden}
.gl-now{position:absolute;top:-6px;bottom:-6px;width:2px;background:currentColor;
  transform:translateX(-1px)}
.gl-now span{position:absolute;top:-18px;left:50%;transform:translateX(-50%);
  font-size:.72rem;font-weight:700;white-space:nowrap}
.gl-ticks{position:relative;height:16px;font-size:.7rem;opacity:.7}
.gl-ticks span{position:absolute;transform:translateX(-50%)}
.gl-ticks span:first-child{transform:none}.gl-ticks span:last-child{transform:translateX(-100%)}
.gl-foot{font-size:.8rem;opacity:.85;margin-top:4px}
.gl-sw{display:inline-block;width:12px;height:10px;border-radius:3px;vertical-align:middle;
  margin-right:4px}
</style>
"""


def makeup_text(gl, sched):
    """One line on light missed while the garden was off, or None if nothing is owed."""
    owed = (gl or {}).get("makeup_owed_s") or 0
    if owed <= 0:
        return None
    night_s = gl.get("makeup_max_night_s") or 0
    slot = ""
    if sched is not None and night_s > 0:
        off = datetime.strptime(sched["off_local"], "%H:%M")
        end = (off + timedelta(seconds=float(night_s))).strftime("%H:%M")
        slot = (f" Make-up keeps the light on after the scheduled OFF, "
                f"{sched['off_local']}–{end} at most each night.")
    if gl.get("makeup"):
        return f"Making up missed light: {fmt_duration(owed)} still owed.{slot}"
    if gl.get("makeup_armed"):
        return f"Make-up on: {fmt_duration(owed)} owed, repaid after the scheduled OFF.{slot}"
    return (f"{fmt_duration(owed)} of scheduled light was missed while the garden was off."
            f"{slot}")


def _pct(dt, day_start):
    return max(0.0, min(100.0, (dt - day_start) / DAY * 100.0))


def _timeline(sched, lit):
    day_start = sched["day_start"]
    now_pct = _pct(sched["now"], day_start)
    parts = []
    edges = [day_start]
    for start, end in sched["windows"]:
        left, right = _pct(start, day_start), _pct(end, day_start)
        live = lit and left <= now_pct < right
        label = f"ON {_hhmm(start)}–{_hhmm(end)}" if right - left > 22 else "ON"
        parts.append(f'<div class="gl-seg{" gl-live" if live else ""}" '
                     f'style="left:{left:.2f}%;width:{right - left:.2f}%" '
                     f'title="Grow light on {_hhmm(start)}–{_hhmm(end)}">{label}</div>')
        edges += [start, end]
    edges.append(day_start + DAY)
    # Dark gaps between ON windows.
    for gap_start, gap_end in zip(edges[::2], edges[1::2]):
        left, right = _pct(gap_start, day_start), _pct(gap_end, day_start)
        if right - left > 8:
            parts.append(f'<div class="gl-dark" style="left:{left:.2f}%;width:{right - left:.2f}%">'
                         "🌙 dark</div>")
    parts.append(f'<div class="gl-now" style="left:{now_pct:.2f}%">'
                 f'<span>now {sched["now"].strftime("%H:%M")}</span></div>')
    ticks = "".join(f'<span style="left:{h / 24 * 100:.2f}%">{h:02d}:00</span>'
                    for h in (0, 6, 12, 18, 24))
    return (f'<div class="gl-track">{"".join(parts)}</div>'
            f'<div class="gl-ticks">{ticks}</div>')


def card_html(gl, received_at, node_live, now=None):
    sched = photoperiod(gl or {}, received_at, now)
    kind, title, detail = headline(gl, sched, node_live)
    lit = bool((gl or {}).get("on"))
    classes = "gl-card" + (" gl-on" if lit else "") + (" gl-warn" if kind == "warn" else "")
    body = [f'<div class="{classes}"><div class="gl-head"><span class="gl-bulb"></span><div>'
            f'<div class="gl-title">{html.escape(title)}</div>'
            f'<div class="gl-sub">{html.escape(detail)}</div></div></div>']
    if sched is not None:
        on_h = sched["on_hours"]
        body.append(_timeline(sched, lit))
        utc = ""
        if gl.get("start_utc") is not None and gl.get("clock"):
            off_utc = gl.get("off_utc")
            if off_utc is None:
                off_utc = (gl["start_utc"] + on_h) % 24
            utc = f" · {int(gl['start_utc']):02d}:00 → {int(off_utc):02d}:00 UTC"
        source = ("follows the NTP clock" if gl.get("clock")
                  else "no NTP — counted from boot, times are estimates")
        body.append(
            '<div class="gl-foot">'
            '<span class="gl-sw" style="background:linear-gradient(90deg,#6d4aff,#c04dff);'
            'box-shadow:0 0 6px rgba(140,90,255,.8)"></span>'
            f'ON {sched["on_local"]} → {sched["off_local"]} ({on_h:g} h) &nbsp; '
            '<span class="gl-sw" style="background:rgba(90,80,140,.28)"></span>'
            f'OFF {sched["off_local"]} → {sched["on_local"]} ({24 - on_h:g} h dark)'
            f' · local time{utc} · {source}</div>')
    owed_line = makeup_text(gl, sched)
    if owed_line:
        body.append(f'<div class="gl-foot">🌗 {html.escape(owed_line)}</div>')
    body.append("</div>")
    return _CSS + "".join(body)


def render_grow_light_panel(state, node_live, on_makeup=None, send_error=None):
    """``on_makeup(action)`` publishes {"cmd": "light_makeup", "action": "on" | "off"}."""
    import streamlit as st

    import explain

    explain.heading("💡 Grow light photoperiod", "grow_light", "subheader")
    gl = state.get("grow_light") or {}
    st.markdown(card_html(gl, state.get("received_at"), node_live), unsafe_allow_html=True)
    owed = gl.get("makeup_owed_s") or 0
    armed = bool(gl.get("makeup_armed"))
    if on_makeup is not None and (owed > 0 or armed):
        st.button(
            "Stop make-up light" if armed else f"Make up {fmt_duration(owed)} of light",
            key="light_makeup",
            disabled=not node_live,
            on_click=on_makeup,
            args=("off" if armed else "on",),
            help='Sends {"cmd": "light_makeup"}. The light stays on after the scheduled OFF '
            "until the missed hours are repaid, a few hours at most each night.",
        )
    if send_error:
        st.error(send_error)
