"""Claude Usage Overlay - an always-on-top, FPS-counter-style usage meter.

Shows your Claude plan usage (current session, this week, and any per-model
weekly limits) using the same endpoint Claude Code's /usage screen uses.
Right-click the overlay for themes, skins, size, opacity and more.

Unofficial project; not affiliated with or endorsed by Anthropic.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

__version__ = "1.0.0"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CONFIG = Path(__file__).with_name("overlay_config.json")
CACHE = Path(__file__).with_name("overlay_cache.json")  # last known usage, shown on startup
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
FEED = CLAUDE_DIR / "usage-overlay-feed.json"  # written by claude_usage_feed.py
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

POLL_SECONDS = 180          # API poll when no status-line feed is available
FEED_POLL_SECONDS = 600     # API poll while the feed is active (per-model limits only)
STALE_SECONDS = 900         # values older than this are greyed out
BACKOFF_SECONDS = 300       # first wait after a 429 (doubles on each repeat)
MAX_BACKOFF_SECONDS = 1800
MIN_MANUAL_REFRESH = 15     # double-click can't hammer the endpoint
MARGIN = 12
MONO = "Consolas" if IS_WINDOWS else "Menlo" if IS_MAC else "DejaVu Sans Mono"

# --- themes (colors) ---------------------------------------------------------
# bg: background, fg: main text, label: row names / timers, dim: stale values,
# low/mid/high: usage colors (<50%, <80%, >=80%), track: empty part of bars/rings.
THEMES = {
    "Midnight": dict(bg="#111111", fg="#e5e5e5", label="#9a9a9a", dim="#555555",
                     low="#4ade80", mid="#facc15", high="#f87171", track="#2a2a2a"),
    "Paper": dict(bg="#f5f5f4", fg="#1c1917", label="#57534e", dim="#a8a29e",
                  low="#15803d", mid="#b45309", high="#b91c1c", track="#d6d3d1", border="#a8a29e"),
    "Terminal": dict(bg="#000000", fg="#33ff33", label="#1f9e1f", dim="#145214",
                     low="#33ff33", mid="#ccff33", high="#ff3333", track="#0a330a"),
    "Synthwave": dict(bg="#1a0b2e", fg="#f5e6ff", label="#b388ff", dim="#4a3570",
                      low="#00f0ff", mid="#ff9e00", high="#ff2a6d", track="#2d1650", border="#ff2a6d"),
    "Dracula": dict(bg="#282a36", fg="#f8f8f2", label="#6272a4", dim="#44475a",
                    low="#50fa7b", mid="#f1fa8c", high="#ff5555", track="#44475a"),
    "Nord": dict(bg="#2e3440", fg="#eceff4", label="#81a1c1", dim="#4c566a",
                 low="#a3be8c", mid="#ebcb8b", high="#bf616a", track="#3b4252"),
    "Solarized": dict(bg="#002b36", fg="#eee8d5", label="#839496", dim="#586e75",
                      low="#859900", mid="#b58900", high="#dc322f", track="#073642"),
    "Ember": dict(bg="#1f1612", fg="#f4e4d8", label="#c9a38a", dim="#5c4638",
                  low="#e8a87c", mid="#e07a4f", high="#d1452f", track="#3a2a22"),
}

# --- skins (layouts) ---------------------------------------------------------
SKINS = ["Classic", "Bars", "Compact", "Rings"]
SIZES = {"Small": 9, "Medium": 11, "Large": 13, "Huge": 16}
OPACITIES = [100, 85, 70, 60, 45, 30]
CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]

DEFAULTS = {
    "theme": "Midnight",
    "skin": "Classic",
    "size": "Large",
    "opacity": 60,
    "mode": "used",          # or "left"
    "show_resets": True,
    "locked": False,
    "corner": "bottom-right",  # None once the user drags it somewhere
}

# Settings that change how the overlay looks (a change triggers a rebuild).
VISUAL_KEYS = ("theme", "skin", "size", "opacity", "show_resets")

SHORT_NAMES = {"Fable": "FBL", "Opus": "OPS", "Sonnet": "SON", "Haiku": "HKU"}


# --- config ------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg["theme"] not in THEMES:
        cfg["theme"] = DEFAULTS["theme"]
    if cfg["skin"] not in SKINS:
        cfg["skin"] = DEFAULTS["skin"]
    if cfg["size"] not in SIZES:
        cfg["size"] = DEFAULTS["size"]
    return cfg


def save_config(cfg):
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_cache():
    try:
        rows = json.loads(CACHE.read_text(encoding="utf-8"))
        for row in rows:
            row["resets_at"] = parse_time(row.get("resets_at"))
        return rows
    except Exception:
        return None


def save_cache(rows):
    try:
        data = [dict(r, resets_at=r["resets_at"].isoformat() if r["resets_at"] else None) for r in rows]
        CACHE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# --- data --------------------------------------------------------------------
def read_feed():
    """Session/week usage written by Claude Code's status line (see claude_usage_feed.py).

    Claude Code hands this data to the status line for free on every reply, so it
    is fresher than the API and never rate limited. It has no per-model limits.
    """
    try:
        limits = json.loads(FEED.read_text(encoding="utf-8")).get("rate_limits") or {}
    except Exception:
        return None
    rows = []
    for field, key, short, name in (("five_hour", "session", "SES", "Session"),
                                    ("seven_day", "week", "WK", "Week")):
        block = limits.get(field) or {}
        if block.get("used_percentage") is None:
            continue
        resets = block.get("resets_at")
        try:
            resets_at = datetime.fromtimestamp(float(resets), timezone.utc) if resets else None
        except (TypeError, ValueError):
            resets_at = parse_time(resets)
        rows.append(dict(key=key, short=short, name=name,
                         pct=block["used_percentage"], resets_at=resets_at))
    return rows


def read_token():
    """Claude Code's OAuth access token (it refreshes this file itself)."""
    cred_file = CLAUDE_DIR / ".credentials.json"
    if cred_file.exists():
        raw = cred_file.read_text(encoding="utf-8")
    elif IS_MAC:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True,
        ).stdout
    else:
        raise FileNotFoundError(cred_file)
    return json.loads(raw)["claudeAiOauth"]["accessToken"]


def fetch_usage():
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {read_token()}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"claude-usage-overlay/{__version__}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return parse_usage(json.loads(resp.read().decode("utf-8")))


def parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_usage(payload):
    """Return a list of rows: dict(key, short, name, pct, resets_at)."""
    rows = []
    for limit in payload.get("limits") or []:
        kind = limit.get("kind")
        pct, resets_at = limit.get("percent"), parse_time(limit.get("resets_at"))
        if kind == "session":
            rows.append(dict(key="session", short="SES", name="Session", pct=pct, resets_at=resets_at))
        elif kind == "weekly_all":
            rows.append(dict(key="week", short="WK", name="Week", pct=pct, resets_at=resets_at))
        elif kind == "weekly_scoped":
            scope = limit.get("scope") or {}
            name = ((scope.get("model") or {}).get("display_name")
                    or (scope.get("surface") or {}).get("display_name") or "Scoped")
            rows.append(dict(key=f"scoped:{name}", short=SHORT_NAMES.get(name, name[:3].upper()),
                             name=f"{name} wk", pct=pct, resets_at=resets_at))
    if rows:
        return rows
    # Fallback for older response shapes.
    for field, key, short, name in (("five_hour", "session", "SES", "Session"),
                                    ("seven_day", "week", "WK", "Week"),
                                    ("seven_day_opus", "scoped:Opus", "OPS", "Opus wk"),
                                    ("seven_day_sonnet", "scoped:Sonnet", "SON", "Sonnet wk")):
        block = payload.get(field)
        if block:
            rows.append(dict(key=key, short=short, name=name, pct=block.get("utilization"),
                             resets_at=parse_time(block.get("resets_at"))))
    return rows


def fmt_countdown(resets_at):
    if resets_at is None:
        return ""
    secs = int((resets_at - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def enable_dpi_awareness():
    """Render crisply on scaled displays (125%, 150%...) instead of being stretched."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --- Windows autostart -------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClaudeUsageOverlay"


def autostart_command():
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass


# --- overlay -----------------------------------------------------------------
class Overlay:
    def __init__(self):
        self.cfg = load_config()
        # Start from the last known values (shown greyed out) until the first refresh.
        self.rows = load_cache() or [
            dict(key="session", short="SES", name="Session", pct=None, resets_at=None),
            dict(key="week", short="WK", name="Week", pct=None, resets_at=None)]
        self.feed_mtime = 0.0
        self.error = None
        self.next_poll = 0.0
        self.last_poll = 0.0
        self.backoff = 0
        self.fetching = False
        self.built_for = None
        self.updaters = []

        enable_dpi_awareness()
        root = self.root = tk.Tk()
        # Pixel sizes below are designed for 96 DPI; scale them on high-DPI screens.
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        root.title("Claude Usage")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.container = None

        self.build_menu()
        self.rebuild()
        # Geometry set before the window is mapped is ignored for overrideredirect
        # windows on Windows, so show it first and then position it.
        root.update()
        self.place()
        root.after(200, self.place)
        self.tick()
        self.keep_on_top()

    def px(self, n):
        return round(n * self.scale)

    @property
    def theme(self):
        return THEMES[self.cfg["theme"]]

    @property
    def font_size(self):
        return SIZES[self.cfg["size"]]

    # --- menu ----------------------------------------------------------------
    def build_menu(self):
        m = self.menu = tk.Menu(self.root, tearoff=0)
        self.vars = {}

        def radio_menu(label, key, options, fmt=str):
            sub = tk.Menu(m, tearoff=0)
            var = self.vars[key] = tk.StringVar(value=str(self.cfg[key]))
            for opt in options:
                sub.add_radiobutton(label=fmt(opt), value=str(opt), variable=var,
                                    command=lambda k=key, o=opt: self.set_option(k, o))
            m.add_cascade(label=label, menu=sub)

        def check(label, key):
            var = self.vars[key] = tk.BooleanVar(value=self.cfg[key])
            m.add_checkbutton(label=label, variable=var,
                              command=lambda: self.set_option(key, var.get()))

        radio_menu("Theme", "theme", THEMES)
        radio_menu("Skin", "skin", SKINS)
        radio_menu("Size", "size", SIZES)
        radio_menu("Opacity", "opacity", OPACITIES, lambda o: f"{o}%")
        radio_menu("Show", "mode", ["used", "left"], lambda o: "% used" if o == "used" else "% left")
        check("Show reset timers", "show_resets")
        m.add_separator()

        pos = tk.Menu(m, tearoff=0)
        for corner in CORNERS:
            pos.add_command(label=f"Snap {corner}", command=lambda c=corner: self.snap(c))
        m.add_cascade(label="Position", menu=pos)
        check("Lock position", "locked")
        if IS_WINDOWS:
            var = self.vars["autostart"] = tk.BooleanVar(value=autostart_enabled())
            m.add_checkbutton(label="Start with Windows", variable=var,
                              command=lambda: set_autostart(var.get()))
        m.add_separator()
        m.add_command(label="Refresh now", command=self.refresh_now)
        m.add_command(label="Exit", command=self.root.destroy)

    def set_option(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        self.built_for = None  # force rebuild
        self.render()

    def show_menu(self, e):
        if IS_WINDOWS:
            self.vars["autostart"].set(autostart_enabled())
        self.menu.tk_popup(e.x_root, e.y_root)

    # --- layout --------------------------------------------------------------
    def rebuild(self):
        t, size = self.theme, self.font_size
        if self.container is not None:
            self.container.destroy()
        self.root.configure(bg=t["bg"])
        self.root.attributes("-alpha", self.cfg["opacity"] / 100)
        self.container = tk.Frame(self.root, bg=t["bg"], padx=self.px(8), pady=self.px(5),
                                  highlightthickness=1 if t.get("border") else 0,
                                  highlightbackground=t.get("border", t["bg"]))
        self.container.pack()
        body = tk.Frame(self.container, bg=t["bg"])
        body.pack()
        self.updaters = [getattr(self, f"build_{self.cfg['skin'].lower()}")(body, row, i)
                         for i, row in enumerate(self.rows)]
        self.status = tk.Label(self.container, text="", font=(MONO, max(8, size - 3)),
                               bg=t["bg"], fg=t["high"], anchor="w")
        self.status.pack(fill="x")
        self.status.pack_forget()
        self.bind_all_widgets(self.root)
        self.built_for = self.layout_signature()
        self.status_shown = False
        self.root.update_idletasks()
        if self.cfg.get("corner"):
            self.root.after(10, self.place)

    def label(self, parent, text="", fg=None, size=None, bold=True, **kw):
        t = self.theme
        font = (MONO, size or self.font_size, "bold" if bold else "normal")
        return tk.Label(parent, text=text, font=font, bg=t["bg"], fg=fg or t["label"], **kw)

    def build_classic(self, body, row, i):
        name = self.label(body, row["short"], anchor="w", width=4)
        name.grid(row=i, column=0, sticky="w")
        pct = self.label(body, anchor="e", width=5)
        pct.grid(row=i, column=1, sticky="e")
        reset = self.label(body, anchor="w", width=8)
        if self.cfg["show_resets"]:
            reset.grid(row=i, column=2, sticky="w", padx=(6, 0))

        def update(text, color, frac, countdown):
            pct.config(text=text, fg=color)
            reset.config(text=f"↻{countdown}" if countdown else "")
        return update

    def build_bars(self, body, row, i):
        t, size = self.theme, self.font_size
        self.label(body, row["short"], anchor="w", width=4).grid(row=i, column=0, sticky="w")
        w, h = self.px(size * 10), self.px(max(6, size - 2))
        bar = tk.Canvas(body, width=w, height=h, bg=t["track"], highlightthickness=0)
        bar.grid(row=i, column=1, padx=(2, 6), pady=2)
        fill = bar.create_rectangle(0, 0, 0, h, width=0, fill=t["low"])
        pct = self.label(body, anchor="e", width=5)
        pct.grid(row=i, column=2, sticky="e")
        reset = self.label(body, anchor="w", width=8, size=max(8, size - 2), bold=False)
        if self.cfg["show_resets"]:
            reset.grid(row=i, column=3, sticky="w", padx=(6, 0))

        def update(text, color, frac, countdown):
            bar.coords(fill, 0, 0, w * frac, h)
            bar.itemconfig(fill, fill=color)
            pct.config(text=text, fg=color)
            reset.config(text=f"↻{countdown}" if countdown else "")
        return update

    def build_compact(self, body, row, i):
        t = self.theme
        if i:
            self.label(body, "·", fg=t["dim"]).pack(side="left", padx=4)
        self.label(body, row["short"]).pack(side="left")
        pct = self.label(body)
        pct.pack(side="left", padx=(4, 0))
        reset = self.label(body, size=max(8, self.font_size - 3), bold=False)
        if self.cfg["show_resets"]:
            reset.pack(side="left", padx=(3, 0))

        def update(text, color, frac, countdown):
            pct.config(text=text, fg=color)
            reset.config(text=f"↻{countdown}" if countdown else "")
        return update

    def build_rings(self, body, row, i):
        t, size = self.theme, self.font_size
        col = tk.Frame(body, bg=t["bg"])
        col.grid(row=0, column=i, padx=4)
        d = self.px(size * 5)
        stroke = self.px(max(3, size // 2))
        ring = tk.Canvas(col, width=d, height=d, bg=t["bg"], highlightthickness=0)
        ring.pack()
        pad = stroke / 2 + 1
        track = ring.create_oval(pad, pad, d - pad, d - pad, outline=t["track"], width=stroke)
        arc = ring.create_arc(pad, pad, d - pad, d - pad, start=90, extent=0,
                              style="arc", outline=t["low"], width=stroke)
        text_id = ring.create_text(d / 2, d / 2, text="--", fill=t["fg"],
                                   font=(MONO, max(8, size - 1), "bold"))
        self.label(col, row["short"], size=max(8, size - 3)).pack()
        reset = self.label(col, size=max(7, size - 4), bold=False)
        if self.cfg["show_resets"]:
            reset.pack()

        def update(text, color, frac, countdown):
            # Tk draws near-360-degree arcs unreliably, so a full ring recolors the track instead.
            full = frac >= 0.995
            ring.itemconfig(track, outline=color if full else t["track"])
            ring.itemconfig(arc, extent=0 if full else -360 * frac, outline=color)
            ring.itemconfig(text_id, text=text, fill=color)
            reset.config(text=countdown)
        return update

    def bind_all_widgets(self, widget):
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.on_drag)
        widget.bind("<ButtonRelease-1>", self.end_drag)
        widget.bind("<Double-Button-1>", lambda e: self.refresh_now())
        widget.bind("<Button-3>", self.show_menu)
        if IS_MAC:
            widget.bind("<Button-2>", self.show_menu)
        for child in widget.winfo_children():
            if not isinstance(child, tk.Menu):
                self.bind_all_widgets(child)

    # --- positioning ---------------------------------------------------------
    def work_area(self):
        left, top = 0, 0
        right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if IS_WINDOWS:
            try:
                rect = (ctypes.c_long * 4)()
                if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, rect, 0):  # SPI_GETWORKAREA
                    left, top, right, bottom = rect
            except Exception:
                pass
        return left, top, right, bottom

    def place(self):
        corner = self.cfg.get("corner")
        if corner:
            w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
            left, top, right, bottom = self.work_area()
            margin = self.px(MARGIN)
            x = left + margin if "left" in corner else right - w - margin
            y = top + margin if "top" in corner else bottom - h - margin
        else:
            x, y = self.cfg.get("x", 0), self.cfg.get("y", 0)
            # Keep the whole overlay on screen when it grows (e.g. an error line appears).
            w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
            left, top, right, bottom = self.monitor_area(x, y)
            x = max(left, min(x, right - w))
            y = max(top, min(y, bottom - h))
        self.root.geometry(f"+{x}+{y}")

    def monitor_area(self, x, y):
        """Work area of the monitor containing (x, y), so it works on any screen."""
        if IS_WINDOWS:
            try:
                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4),
                                ("rcWork", ctypes.c_long * 4), ("dwFlags", ctypes.c_ulong)]
                user32 = ctypes.windll.user32
                user32.MonitorFromPoint.restype = ctypes.c_void_p
                user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                point = ctypes.c_longlong((y << 32) | (x & 0xFFFFFFFF))  # POINT passed by value
                monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
                info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
                if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    return tuple(info.rcWork)
            except Exception:
                pass
        return self.work_area()

    def snap(self, corner):
        self.cfg["corner"] = corner
        save_config(self.cfg)
        self.place()

    def start_drag(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if self.cfg["locked"]:
            return
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        self.cfg["corner"] = None

    def end_drag(self, e):
        if self.cfg["locked"] or self.cfg.get("corner"):
            return
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def keep_on_top(self):
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.after(3000, self.keep_on_top)

    # --- polling -------------------------------------------------------------
    def refresh_now(self):
        # Manual refresh is ignored while rate limited, and throttled otherwise.
        if not self.backoff:
            self.next_poll = min(self.next_poll, self.last_poll + MIN_MANUAL_REFRESH)

    def feed_active(self):
        return time.time() - self.feed_mtime < STALE_SECONDS

    def check_feed(self):
        """Apply new session/week numbers written by Claude Code's status line."""
        try:
            mtime = FEED.stat().st_mtime
        except OSError:
            return
        if mtime <= self.feed_mtime:
            return
        self.feed_mtime = mtime
        feed_rows = read_feed()
        if not feed_rows:
            return
        by_key = {r["key"]: r for r in self.rows}
        for new in feed_rows:
            new["updated"] = mtime
            if new["key"] in by_key:
                by_key[new["key"]].update(pct=new["pct"], resets_at=new["resets_at"], updated=mtime)
            else:
                self.rows.insert(0 if new["key"] == "session" else 1, new)
        save_cache(self.rows)

    def poll(self):
        # With the status-line feed active, the API is only needed for per-model limits.
        delay = FEED_POLL_SECONDS if self.feed_active() else POLL_SECONDS
        rows, error = None, None
        try:
            rows = fetch_usage() or None
            if rows is None:
                error = "no usage data"
            self.backoff = 0
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Back off harder on every consecutive 429; honor Retry-After if given.
                self.backoff = min(MAX_BACKOFF_SECONDS, max(BACKOFF_SECONDS, self.backoff * 2))
                try:
                    retry_after = int(exc.headers.get("Retry-After") or 0)
                except ValueError:
                    retry_after = 0
                delay = max(self.backoff, retry_after)
                error = "rate limited"
            else:
                error = "token expired - open Claude Code" if exc.code == 401 else f"HTTP {exc.code}"
        except (FileNotFoundError, subprocess.CalledProcessError, KeyError):
            error = "not logged in to Claude Code"
        except Exception:
            error = "offline"
        self.next_poll = time.monotonic() + delay
        self.fetching = False
        self.root.after(0, self.on_data, rows, error)

    def on_data(self, rows, error):
        if rows is not None:
            now = time.time()
            old = {r["key"]: r for r in self.rows}
            for row in rows:
                row["updated"] = now
                # Keep a status-line value if it is newer than this API response.
                prev = old.get(row["key"])
                if prev and prev.get("updated", 0) > now:
                    row.update(pct=prev["pct"], resets_at=prev["resets_at"], updated=prev["updated"])
            self.rows = rows
            save_cache(rows)
        self.error = error
        self.render()

    def render(self):
        if self.built_for != self.layout_signature():
            self.rebuild()
        t = self.theme
        now = datetime.now(timezone.utc)
        any_stale = False
        for row, update in zip(self.rows, self.updaters):
            used = row["pct"]
            if used is None:
                update("--", t["dim"], 0, "")
                any_stale = True
                continue
            if row["resets_at"] and row["resets_at"] <= now:
                used = 0  # the window has reset since we last heard
            stale = time.time() - row.get("updated", 0) > STALE_SECONDS
            any_stale |= stale
            shown = used if self.cfg["mode"] == "used" else max(0, 100 - used)
            color = t["dim"] if stale else t["high"] if used >= 80 else t["mid"] if used >= 50 else t["low"]
            update(f"{round(shown)}%", color, min(1, shown / 100), fmt_countdown(row["resets_at"]))
        # API errors only matter to the viewer while some value is out of date.
        error = self.error if any_stale else None
        if error == "rate limited":
            wait = max(0, int(self.next_poll - time.monotonic()))
            error = f"rate limited, retry {wait // 60}:{wait % 60:02d}"
        self.status.config(text=f"! {error}" if error else "")
        if bool(error) != self.status_shown:
            self.status_shown = bool(error)
            if self.status_shown:
                self.status.pack(fill="x")
            else:
                self.status.pack_forget()
            self.root.update_idletasks()
            self.place()

    def layout_signature(self):
        return tuple(r["key"] for r in self.rows), tuple(self.cfg[k] for k in VISUAL_KEYS)

    def tick(self):
        self.check_feed()
        if not self.fetching and time.monotonic() >= self.next_poll:
            self.fetching = True
            self.last_poll = time.monotonic()
            threading.Thread(target=self.poll, daemon=True).start()
        self.render()
        self.root.after(1000, self.tick)


if __name__ == "__main__":
    Overlay().root.mainloop()
