#!/usr/bin/env python3
"""
pi-fan-monitor.py - terminal dashboard for the CPU temperature and fan of a
Raspberry Pi, with a log file (CSV), a chart (SVG) and a built-in stress test.

Usage:
  python3 pi-fan-monitor.py                  live monitoring (s = toggle stress, q = quit)
  python3 pi-fan-monitor.py --stress         automatic: 1 min idle, 5 min stress, 3 min cool-down
  python3 pi-fan-monitor.py --help           all options

See README.md for details. Python 3 standard library only; Linux only.
"""
__version__ = "1.0.0"

import argparse
import csv
import glob
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from multiprocessing import Process, cpu_count

THERMAL = "/sys/class/thermal/thermal_zone0/temp"
FREQ = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
ARGON_CONF = "/etc/argononed.conf"
ARGON_DEFAULT = [(55, 30), (60, 55), (65, 100)]

# ================================================================ sensors

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cpu_temp():
    v = read(THERMAL)
    return int(v) / 1000 if v else None


def cpu_freq():
    v = read(FREQ)
    return int(v) // 1000 if v else None


def cpu_times_all():
    """{'cpu': (idle, total), 'cpu0': ..., ...}"""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            p = line.split()
            vals = list(map(int, p[1:]))
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            out[p[0]] = (idle, sum(vals))
    return out


def loads(prev, cur):
    res = {}
    for k, (idle, total) in cur.items():
        if k in prev:
            dt = total - prev[k][1]
            res[k] = 100 * (1 - (idle - prev[k][0]) / dt) if dt else 0.0
    return res


def throttled():
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def throttle_status(v):
    """(text, severity 0=ok 1=warning 2=problem)"""
    if v is None:
        return "unknown", 1
    if v & 0x4:
        return "THROTTLED", 2
    if v & 0x1:
        return "undervoltage", 2
    if v & 0x8:
        return "temp limit", 1
    if v & 0x2:
        return "freq capped", 1
    if v & 0xF0000:
        return "OK (not earlier)", 1
    return "OK", 0


def load_curve():
    curve = []
    txt = read(ARGON_CONF)
    if txt:
        for line in txt.splitlines():
            line = line.split("#")[0].strip()
            if "=" not in line:
                continue
            try:
                t, s = line.split("=", 1)
                curve.append((float(t), max(0, min(100, int(float(s))))))
            except ValueError:
                pass
    source = "argononed.conf" if curve else "Argon default"
    return sorted(curve or ARGON_DEFAULT, reverse=True), source


def fan_from_curve(temp, curve):
    if temp is None:
        return None
    for t, s in curve:
        if temp >= t:
            return s
    return 0


def argon_daemon():
    """True/False if systemd knows the argononed service, otherwise None."""
    if not shutil.which("systemctl"):
        return None
    try:
        out = subprocess.run(["systemctl", "is-active", "argononed"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        return out == "active" if out in ("active", "inactive", "failed") else None
    except Exception:
        return None


def find_hwmon_pwm(any_device=False):
    """Pi 5 fan header (pwm-fan driver). Returns the path to pwm1."""
    for p in glob.glob("/sys/class/hwmon/hwmon*/pwm1"):
        name = read(os.path.join(os.path.dirname(p), "name")) or ""
        if read(p) is not None and (any_device or "fan" in name):
            return p
    return None


def fan_rpm(pwm_path):
    if not pwm_path:
        return None
    v = read(os.path.join(os.path.dirname(pwm_path), "fan1_input"))
    return int(v) if v and v.lstrip("-").isdigit() else None

# ================================================================ stress test

def _burn():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # otherwise it inherits the stop handler from main
    x = 1
    while True:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF


class Stress:
    def __init__(self):
        self.proc = None
        self.procs = []
        self.method = "stress-ng" if shutil.which("stress-ng") else "python"

    @property
    def running(self):
        return bool(self.proc or self.procs)

    def start(self):
        if self.running:
            return
        if self.method == "stress-ng":
            self.proc = subprocess.Popen(
                ["stress-ng", "--cpu", str(cpu_count()), "--cpu-method", "matrixprod"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        else:
            self.procs = [Process(target=_burn, daemon=True) for _ in range(cpu_count())]
            for p in self.procs:
                p.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.proc = None
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
        self.procs = []

# ================================================================ drawing

RESET, BOLD = "\033[0m", "\033[1m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def rgb(c):
    return f"\033[38;2;{c[0]};{c[1]};{c[2]}m"


C_BORDER = (88, 102, 128)
C_TEXT = (200, 205, 215)
C_DIM = (95, 100, 115)
C_ACCENT = (110, 190, 255)
C_EMPTY = (55, 58, 70)
C_FAN = [(0, (60, 140, 220)), (0.6, (90, 200, 240)), (1, (170, 240, 255))]
C_LOAD = [(0, (120, 90, 210)), (0.6, (200, 100, 220)), (1, (250, 130, 180))]
C_HEAT = [(0, (70, 200, 120)), (0.45, (220, 210, 70)), (0.7, (245, 150, 50)), (1, (235, 60, 60))]


def grad(stops, x):
    x = max(0.0, min(1.0, x))
    for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
        if x <= x1:
            f = (x - x0) / (x1 - x0) if x1 > x0 else 0
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return stops[-1][1]


def heat(t):
    """colour for a temperature: 40 green, ~58 yellow, ~68 orange, 80+ red"""
    return grad(C_HEAT, ((t or 0) - 40) / 40)


def vlen(s):
    return len(ANSI.sub("", s))


def fit(s, w):
    """Cut or pad an ANSI string to visible width w."""
    if vlen(s) <= w:
        return s + " " * (w - vlen(s))
    out, n, i = "", 0, 0
    while i < len(s) and n < w:
        m = ANSI.match(s, i)
        if m:
            out += m.group()
            i = m.end()
        else:
            out += s[i]
            n += 1
            i += 1
    return out + RESET


def box(w, h, title, lines, accent=C_ACCENT):
    b = rgb(C_BORDER)
    t = f"{b}╭─┤{RESET}{BOLD}{rgb(accent)} {title} {RESET}{b}├"
    top = t + "─" * max(0, w - 2 - vlen(t) + 1) + "╮" + RESET
    out = [fit(top, w)]
    for i in range(h - 2):
        content = lines[i] if i < len(lines) else ""
        out.append(f"{b}│{RESET}{fit(content, w - 2)}{b}│{RESET}")
    out.append(f"{b}╰{'─' * (w - 2)}╯{RESET}")
    return out


def meter(pct, width, stops):
    pct = max(0, min(100, pct or 0))
    filled = round(pct / 100 * width)
    s = ""
    for i in range(width):
        if i < filled:
            s += rgb(grad(stops, i / max(1, width - 1))) + "■"
        else:
            s += rgb(C_EMPTY) + "■"
    return s + RESET


# braille dots, per side, bottom to top
_UP = ([0x40, 0x04, 0x02, 0x01], [0x80, 0x20, 0x10, 0x08])
_DOWN = ([0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80])


def braille(values, width, height, lo, hi, colorfn, down=False):
    """Filled braille graph (2 samples per character, 4 dots high), newest on the right."""
    vals = list(values)[-width * 2:]
    vals = [None] * (width * 2 - len(vals)) + vals
    dots = 4 * height
    span = (hi - lo) or 1
    n = []
    for v in vals:
        if v is None:
            n.append(None)
        else:
            k = round((v - lo) / span * dots)
            n.append(max(1 if v > lo else 0, min(dots, k)))
    order = _DOWN if down else _UP
    rows = range(height) if down else range(height - 1, -1, -1)
    lines = []
    for r in rows:
        level = lo + span * (r + 0.5) / height
        s = rgb(colorfn(level))
        for c in range(width):
            bits = 0
            for side, cnt in zip(order, (n[2 * c], n[2 * c + 1])):
                if cnt is None:
                    continue
                for i in range(max(0, min(4, cnt - 4 * r))):
                    bits |= side[i]
            s += chr(0x2800 + bits) if bits else " "
        lines.append(s + RESET)
    return lines


_UP_BLK = " ▁▂▃▄▅▆▇█"


def blocks(values, width, height, lo, hi, colorfn, down=False):
    """Filled block graph (1 sample per character, 8 steps per row).
    Works in any font, including ones where braille looks dotted."""
    vals = list(values)[-width:]
    vals = [None] * (width - len(vals)) + vals
    span = (hi - lo) or 1
    h8 = [None if v is None else max(1 if v > lo else 0, min(8 * height, round((v - lo) / span * 8 * height)))
          for v in vals]
    rows = range(height) if down else range(height - 1, -1, -1)
    lines = []
    for r in rows:
        level = lo + span * (r + 0.5) / height
        s = rgb(colorfn(level))
        for h in h8:
            f = 0 if h is None else max(0, min(8, h - 8 * r))
            if down:  # top-down: only full/half/eighth blocks exist
                s += "█" if f >= 6 else "▀" if f >= 3 else "▔" if f >= 1 else " "
            else:
                s += _UP_BLK[f]
        lines.append(s + RESET)
    return lines


def mmss(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def render(st):
    cols, rows_ = shutil.get_terminal_size((100, 30))
    if st.get("size") != (cols, rows_):  # window resized: clear everything first
        st["size"] = (cols, rows_)
        sys.stdout.write("\033[2J")
    if cols < 64 or rows_ < 22:
        msg = f"Please enlarge the window (at least 64x22, now {cols}x{rows_})"
        sys.stdout.write("\033[H\033[J" + msg)
        sys.stdout.flush()
        return
    r = st["last"]
    temps = [x for x in st["hist_temp"] if x is not None]
    ncores = len(st["core_loads"])

    # ---- layout
    per_row = max(1, min(4, (cols - 2) // 30))
    while ncores and ncores % per_row and per_row > 1:
        per_row -= 1
    cores_h = math.ceil(ncores / per_row) + 2 if ncores else 0
    remaining = rows_ - 1 - cores_h
    top_h = max(10, round(remaining * 0.58))
    mid_h = remaining - top_h
    info_w = 38 if cols >= 100 else 32
    temp_w = cols - info_w

    # ---- temperature graph
    lo = math.floor((min(temps) if temps else 35) / 5) * 5 - 5
    hi = math.ceil((max(temps) if temps else 55) / 5) * 5 + 5
    hi = max(hi, lo + 20)
    gh, gw = top_h - 2, temp_w - 2 - 6
    G = braille if st["braille"] else blocks
    graph = G(st["hist_temp"], gw, gh, lo, hi, heat)
    tl = []
    for i, line in enumerate(graph):
        val = hi - (hi - lo) * i / max(1, gh - 1)
        lab = f"{val:3.0f}° " if i in (0, gh // 2, gh - 1) else ""
        tl.append(f"{rgb(C_DIM)}{lab:>5}{RESET}{rgb(C_BORDER)}┤{RESET}{line}")
    temp_box = box(temp_w, top_h, f"temperature  {st['host']}", tl, (255, 150, 90))

    # ---- info panel
    iw = info_w - 2
    mw = iw - 2
    tc = rgb(heat(r["temp"]))
    t_txt = f"{r['temp']:.1f}°C" if r["temp"] is not None else "n/a"
    stat_txt, sev = throttle_status(r["throttle"])
    stat_c = [(90, 210, 130), (235, 200, 80), (240, 80, 80)][sev]
    ph = r["phase"]
    ph_c = (240, 110, 90) if ph == "stress" else (110, 190, 255) if ph == "cooldown" else (90, 210, 130)
    fan_src = ((f"{r['rpm']} rpm" if r.get("rpm") is not None else "measured") if st["fanmode"] == "pwm"
               else "curve" if st["fanmode"] == "argon" else "n/a")
    D, T = rgb(C_DIM), rgb(C_TEXT)
    info = [
        f" {D}phase{RESET}  {BOLD}{rgb(ph_c)}● {ph.upper()}{RESET}"
        + (f"  {D}{mmss(st['phase_left'])} left{RESET}" if st["phase_left"] is not None else ""),
        f" {D}time{RESET}   {T}{mmss(r['t'])}{RESET}   {D}status{RESET} {rgb(stat_c)}{stat_txt}{RESET}",
        "",
        f" {T}CPU-temp{RESET}  {BOLD}{tc}{t_txt:>8}{RESET}",
        " " + meter(((r["temp"] or 0) - 30) / 55 * 100, mw, C_HEAT),
        (f" {D}min{RESET} {T}{min(temps):.1f}{RESET}  {D}avg{RESET} {T}{sum(temps) / len(temps):.1f}"
         f"{RESET}  {D}max{RESET} {T}{max(temps):.1f}{RESET}") if temps else "",
        "",
        f" {T}Fan{RESET}  {BOLD}{rgb(grad(C_FAN, (r['fan'] or 0) / 100))}"
        f"{(str(r['fan']) + '%') if r['fan'] is not None else '–':>6}{RESET}"
        f"  {D}({fan_src}){RESET}",
        " " + meter(r["fan"], mw, C_FAN),
        f" {T}Load{RESET} {BOLD}{rgb(grad(C_LOAD, r['load'] / 100))}{r['load']:>5.0f}%{RESET}"
        f"  {D}clock{RESET} {T}{r['freq'] or 0} MHz{RESET}",
        " " + meter(r["load"], mw, C_LOAD),
        "",
    ]
    if st["fanmode"] == "pwm":
        info += [f" {D}fan on Pi 5 fan header{RESET}",
                 f" {D}curve: config.txt (fan_temp*){RESET}"]
    elif st["fanmode"] == "argon":
        info += [f" {D}argon-daemon{RESET} " + (
                     f"{rgb((90, 210, 130))}● active{RESET}" if st["daemon"] is True else
                     f"{rgb((240, 80, 80))}● OFF{RESET}" if st["daemon"] is False else f"{D}unknown{RESET}"),
                 f" {D}fan curve ({st['curve_src']}){RESET}"]
    else:
        info += [f" {D}no fan found{RESET}"]
    line = " "
    for t, sp in (sorted(st["curve"]) if st["fanmode"] == "argon" else []):
        item = f"{rgb(heat(t))}{t:.0f}°{RESET}{D}→{RESET}{T}{sp}%{RESET}  "
        if vlen(line) + vlen(item) > iw:
            info.append(line)
            line = " "
        line += item
    if line.strip():
        info.append(line)
    if len(info) > top_h - 2:  # small window: drop empty lines
        info = [l for l in info if l]
    info_box = box(info_w, top_h, "info", info, C_ACCENT)

    # ---- fan / load, mirrored
    mh = mid_h - 2
    up_h = mh // 2
    dn_h = mh - up_h
    mw2 = cols - 2 - 6
    up = G(st["hist_fan"], mw2, up_h, 0, 100, lambda v: grad(C_FAN, v / 100))
    dn = G(st["hist_load"], mw2, dn_h, 0, 100, lambda v: grad(C_LOAD, v / 100), down=True)
    ml = []
    for i, line in enumerate(up):
        lab = "100%" if i == 0 else ("fan" if i == up_h - 1 else "")
        ml.append(f"{rgb(C_DIM)}{lab:>5}{RESET}{rgb(C_BORDER)}┤{RESET}{line}")
    for i, line in enumerate(dn):
        lab = "load" if i == 0 else ("100%" if i == dn_h - 1 else "")
        ml.append(f"{rgb(C_DIM)}{lab:>5}{RESET}{rgb(C_BORDER)}┤{RESET}{line}")
    mid_box = box(cols, mid_h, f"{rgb(grad(C_FAN, .7))}fan ▲{RESET}  "
                  f"{BOLD}{rgb(grad(C_LOAD, .7))}cpu-load ▼", ml)

    # ---- cores
    frame = [a + b for a, b in zip(temp_box, info_box)] + mid_box
    if ncores:
        cw = (cols - 2) // per_row
        cl = []
        keys = sorted(st["core_loads"], key=lambda k: int(k[3:]))
        for i in range(0, ncores, per_row):
            line = ""
            for k in keys[i:i + per_row]:
                v = st["core_loads"][k]
                bw = cw - 14
                line += fit(f" {rgb(C_TEXT)}C{k[3:]:<2}{RESET} {meter(v, bw, C_LOAD)} "
                            f"{rgb(C_TEXT)}{v:4.0f}%{RESET}", cw)
            cl.append(line)
        frame += box(cols, cores_h, "cores", cl, (200, 130, 240))

    # ---- status line
    K = lambda key, txt: f"{BOLD}{rgb(C_ACCENT)}{key}{RESET}{rgb(C_DIM)} {txt}{RESET}"
    stress_lbl = "stop stress" if st["stress"].running else "start stress"
    keys = [K("q", "quit")] + ([] if st["auto"] else [K("s", stress_lbl)])
    status = "  " + "   ".join(keys) + f"   {rgb(C_DIM)}log → {st['csv']}{RESET}"
    frame.append(fit(status, cols - 1))

    sys.stdout.write("\033[H" + "\n".join(fit(l, cols) for l in frame[:-1]) + "\n" + frame[-1])
    sys.stdout.flush()

# ================================================================ SVG report

def write_svg(path, rows, title):
    rows = [r for r in rows if r["temp"] is not None]
    if len(rows) < 2:
        return False
    W, H, ml, mr = 1000, 560, 64, 64
    tmax = rows[-1]["t"] or 1
    X = lambda t: ml + t / tmax * (W - ml - mr)
    temps = [r["temp"] for r in rows]
    lo = math.floor(min(temps) / 5) * 5 - 5
    hi = math.ceil(max(temps) / 5) * 5 + 5
    p1, p2 = (60, 300), (350, 500)
    Y1 = lambda v: p1[1] - (v - lo) / (hi - lo) * (p1[1] - p1[0])
    Y2 = lambda v: p2[1] - v / 100 * (p2[1] - p2[0])
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="system-ui,sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>',
         f'<text x="{ml}" y="30" font-size="17" font-weight="600">{title}</text>']
    start = None
    for r in rows + [{"t": tmax, "phase": "end"}]:
        if r["phase"] == "stress" and start is None:
            start = r["t"]
        elif r["phase"] != "stress" and start is not None:
            for (a, b) in (p1, p2):
                s.append(f'<rect x="{X(start):.1f}" y="{a}" width="{X(r["t"]) - X(start):.1f}" '
                         f'height="{b - a}" fill="#fde2d8"/>')
            s.append(f'<text x="{(X(start) + X(r["t"])) / 2:.1f}" y="{p1[0] + 14}" '
                     f'text-anchor="middle" fill="#c0392b">stress test</text>')
            start = None
    for v in range(int(lo), int(hi) + 1, 5):
        y = Y1(v)
        s.append(f'<line x1="{ml}" x2="{W - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eee"/>')
        s.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#666">{v}°</text>')
    for v in range(0, 101, 25):
        y = Y2(v)
        s.append(f'<line x1="{ml}" x2="{W - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eee"/>')
        s.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#666">{v}%</text>')
    step = max(30, int(tmax / 10 // 30 * 30) or 30)
    for t in range(0, int(tmax) + 1, step):
        s.append(f'<text x="{X(t):.1f}" y="{p2[1] + 18}" text-anchor="middle" fill="#666">{mmss(t)}</text>')
    s.append(f'<text x="{W / 2}" y="{p2[1] + 40}" text-anchor="middle" fill="#666">time (min:sec)</text>')

    def poly(pts, color, width=2, dash=""):
        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="{width}"{da}/>'

    s.append(poly([(X(r["t"]), Y1(r["temp"])) for r in rows], "#e4572e", 2.2))
    s.append(poly([(X(r["t"]), Y2(r["load"])) for r in rows], "#999", 1.2, "4 3"))
    fr = [r for r in rows if r["fan"] is not None]
    if fr:
        s.append(poly([(X(r["t"]), Y2(r["fan"])) for r in fr], "#2e86de", 2.2))
    s.append(f'<text x="{ml}" y="{p1[0] - 8}" font-weight="600" fill="#e4572e">CPU temperature</text>')
    s.append(f'<text x="{ml}" y="{p2[0] - 8}" font-weight="600">'
             f'<tspan fill="#2e86de">Fan %</tspan>  <tspan fill="#999">CPU load % (dashed)</tspan></text>')
    s.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(s))
    return True


def summary(rows):
    print(f"\n{BOLD}Summary per phase{RESET}")
    phases = []
    for r in rows:
        if r["phase"] not in phases:
            phases.append(r["phase"])
    for ph in phases:
        rr = [r for r in rows if r["phase"] == ph and r["temp"] is not None]
        if not rr:
            continue
        t = [r["temp"] for r in rr]
        f = [r["fan"] for r in rr if r["fan"] is not None]
        changes = sum(1 for a, b in zip(f, f[1:]) if a != b)
        print(f"  {ph:<9} temp min {min(t):5.1f}  max {max(t):5.1f}  end {t[-1]:5.1f} °C"
              + (f" | fan max {max(f)}%, {changes} speed changes" if f else ""))
    thr = [r["throttle"] for r in rows if r["throttle"]]
    print("  Throttling: " + ("YES, the Pi was throttled by heat" if any(v & 0x4 for v in thr)
                              else "did not occur"))

# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description="monitor for the CPU temperature and fan of a Raspberry Pi")
    ap.add_argument("--version", action="version", version=f"pi-fan-monitor {__version__}")
    ap.add_argument("--interval", type=float, default=1, help="seconds between measurements (default 1)")
    ap.add_argument("--stress", action="store_true", help="run the automatic stress test sequence")
    ap.add_argument("--idle", type=int, default=60, help="seconds of idle measurement before stress (default 60)")
    ap.add_argument("--load", type=int, default=300, help="seconds of stress (default 300)")
    ap.add_argument("--cool", type=int, default=180, help="seconds of cool-down after stress (default 180)")
    ap.add_argument("--fan", choices=["auto", "curve", "pwm"], default="auto",
                    help="fan source: auto = Pi 5 fan header if present, otherwise the Argon curve; "
                         "curve = always the Argon curve; pwm = any hwmon pwm1")
    ap.add_argument("--braille", action="store_true",
                    help="finer braille graphs (only looks good if your font renders braille well)")
    ap.add_argument("--duration", type=int, default=0, help="stop after N seconds (without --stress)")
    a = ap.parse_args()

    host = socket.gethostname()
    base = f"pi-fan-monitor_{host}_{datetime.now():%Y%m%d-%H%M%S}"
    phases = [("idle", a.idle), ("stress", a.load), ("cooldown", a.cool)] if a.stress else None
    curve, curve_src = load_curve()
    stress = Stress()
    pwm = None if a.fan == "curve" else find_hwmon_pwm(any_device=a.fan == "pwm")
    fanmode = ("pwm" if pwm else
               "argon" if a.fan == "curve" or os.path.exists(ARGON_CONF) else "none")
    st = {"host": host, "csv": base + ".csv", "curve": curve, "curve_src": curve_src,
          "pwm": pwm, "fanmode": fanmode,
          "braille": a.braille, "daemon": argon_daemon() if fanmode == "argon" else None,
          "stress": stress, "auto": a.stress,
          "hist_temp": deque(maxlen=1200), "hist_fan": deque(maxlen=1200),
          "hist_load": deque(maxlen=1200), "core_loads": {}}
    rows = []
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))

    tty_old = None
    if sys.stdin.isatty():
        import termios
        import tty
        tty_old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    f = open(base + ".csv", "w", newline="")
    w = csv.writer(f)
    w.writerow(["time", "sec", "phase", "temp_c", "fan_pct", "fan_rpm", "load_pct", "freq_mhz", "throttled"])
    sys.stdout.write("\033[?1049h\033[?25l\033[H\033[J")  # alternate screen, hide cursor
    prev = cpu_times_all()
    t0 = nxt = time.time()
    try:
        while not stop["now"]:
            el = time.time() - t0
            left = None
            if phases:
                acc, phase = 0, None
                for name, dur in phases:
                    if el < acc + dur:
                        phase, left = name, acc + dur - el
                        break
                    acc += dur
                if phase is None:
                    break
                (stress.start if phase == "stress" else stress.stop)()
            else:
                if a.duration and el >= a.duration:
                    break
                phase = "stress" if stress.running else "monitor"

            cur = cpu_times_all()
            ld = loads(prev, cur)
            prev = cur
            temp = cpu_temp()
            if st["pwm"]:
                v = read(st["pwm"])
                fan = round(int(v) / 255 * 100) if v else None
            else:
                fan = fan_from_curve(temp, curve) if fanmode == "argon" else None
            r = {"t": el, "phase": phase, "temp": temp, "fan": fan, "rpm": fan_rpm(st["pwm"]), "load": ld.get("cpu", 0.0),
                 "freq": cpu_freq(), "throttle": throttled()}
            rows.append(r)
            st["hist_temp"].append(temp)
            st["hist_fan"].append(fan)
            st["hist_load"].append(r["load"])
            st["core_loads"] = {k: v for k, v in ld.items() if k != "cpu"}
            st["last"] = r
            st["phase_left"] = left
            if fanmode == "argon" and len(rows) % 15 == 0:
                st["daemon"] = argon_daemon()
            w.writerow([datetime.now().isoformat(timespec="seconds"), round(el, 1), phase,
                        temp, fan, r["rpm"], round(r["load"], 1), r["freq"],
                        hex(r["throttle"]) if r["throttle"] is not None else ""])
            f.flush()
            render(st)

            nxt += a.interval
            while not stop["now"] and time.time() < nxt:
                if tty_old is not None:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if ready:
                        key = sys.stdin.read(1).lower()
                        if key == "q":
                            stop["now"] = True
                        elif key == "s" and not phases:
                            stress.stop() if stress.running else stress.start()
                else:
                    time.sleep(0.05)
    finally:
        stress.stop()
        f.close()
        sys.stdout.write("\033[?25h\033[?1049l")
        if tty_old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, tty_old)
        title = f"{host} — temperature and fan ({datetime.now():%Y-%m-%d %H:%M})"
        if write_svg(base + ".svg", rows, title):
            print(f"Chart saved: {base}.svg")
        print(f"Measurements saved: {base}.csv")
        if rows:
            summary(rows)


if __name__ == "__main__":
    main()
