import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont
import pystray

from display import apply_mode, enumerate_displays, enumerate_modes, get_current_mode, mode_label
from localization import detect_language, set_language, t, theme_definitions
from power import PowerWatcher, is_on_ac

APP_NAME = "ScreenSwitcher"
APP_VERSION = "v1.1.0"
GITHUB_REPO = "marquetas/ScreenSwitcher"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

MUTEX_NAME = "Local\\ScreenSwitcherMutex"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

IS_AUTOSTART = "--autostart" in sys.argv

APPDATA_ROOT = os.getenv("APPDATA") or str(Path.home())
CONFIG_DIR = Path(APPDATA_ROOT) / APP_NAME
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.json"

kernel32 = ctypes.windll.kernel32
ERROR_ALREADY_EXISTS = 183

try:
    dwmapi = ctypes.windll.dwmapi
except Exception:
    dwmapi = None


def default_config() -> dict:
    return {
        "ui": {
            "autostart": False,
            "start_minimized": False,
            "minimize_to_tray": True,
            "language": detect_language(),
            "theme": "light",
            "check_updates": True,
            "update_interval_days": 7,
        },
        "displays": {},
        "last_display": "",
    }


def load_config() -> dict:
    cfg = default_config()
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg["ui"].update(loaded.get("ui", {}) or {})
                cfg["displays"].update(loaded.get("displays", {}) or {})
                cfg["last_display"] = loaded.get("last_display", cfg["last_display"])
        except Exception:
            pass

    if cfg["ui"].get("theme") not in theme_definitions():
        cfg["ui"]["theme"] = "light"
    if cfg["ui"].get("language") not in ("es", "en"):
        cfg["ui"]["language"] = detect_language()

    try:
        cfg["ui"]["update_interval_days"] = int(cfg["ui"].get("update_interval_days", 7))
    except Exception:
        cfg["ui"]["update_interval_days"] = 7

    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_launch_command(include_autostart_flag: bool = False) -> str:
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        script = Path(__file__).resolve()
        cmd = f'"{sys.executable}" "{script}"'

    if include_autostart_flag:
        cmd += " --autostart"
    return cmd


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_launch_command(True))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def safe_mode_tuple(value) -> Optional[Tuple[int, int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        w, h, hz = int(value[0]), int(value[1]), int(value[2])
        if w > 0 and h > 0 and hz > 0:
            return w, h, hz
    except Exception:
        pass
    return None


def hz_color(hz: Optional[int]) -> str:
    if hz is None or hz <= 0:
        return "#94a3b8"
    if hz < 60:
        return "#22c55e"
    if hz == 60:
        return "#38bdf8"
    if hz <= 75:
        return "#06b6d4"
    if hz <= 90:
        return "#a855f7"
    if hz <= 120:
        return "#f59e0b"
    if hz <= 144:
        return "#fb7185"
    if hz <= 165:
        return "#f97316"
    if hz <= 240:
        return "#ef4444"
    return "#e879f9"


def version_tuple(tag: str) -> Tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", tag)]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def is_newer_version(latest_tag: str, current_tag: str = APP_VERSION) -> bool:
    return version_tuple(latest_tag) > version_tuple(current_tag)


def github_request(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ScreenSwitcher"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_url(url: str, target: Path, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ScreenSwitcher"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(target, "wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


class App:
    def __init__(self):
        self._mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if not self._mutex:
            raise RuntimeError("No se pudo crear el mutex de la aplicación.")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self._mutex)
            self._mutex = None
            raise SystemExit(0)

        self.cfg = load_config()
        self.language = self.cfg["ui"].get("language", detect_language())
        self.theme_name = self.cfg["ui"].get("theme", "light")
        set_language(self.language)

        self.themes = theme_definitions()
        self.theme = self.themes.get(self.theme_name, self.themes["light"])

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(t("app_title"))
        self.root.geometry("960x660")
        self.root.minsize(880, 600)

        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self._set_window_icon()

        self.displays: List[dict] = []
        self.display_modes: Dict[str, List[Tuple[int, int, int]]] = {}
        self.display_lookup: Dict[str, str] = {}

        self.selected_display_label = tk.StringVar()
        self.display_enabled = tk.BooleanVar(value=True)
        self.ac_mode = tk.StringVar()
        self.battery_mode = tk.StringVar()

        self.autostart = tk.BooleanVar(value=bool(self.cfg["ui"].get("autostart", False)))
        self.start_minimized = tk.BooleanVar(value=bool(self.cfg["ui"].get("start_minimized", False)))
        self.minimize_to_tray = tk.BooleanVar(value=bool(self.cfg["ui"].get("minimize_to_tray", True)))

        self.theme_var = tk.StringVar(value=self.theme_name)
        self.language_var = tk.StringVar(value=self.language)

        self.check_updates_enabled = tk.BooleanVar(value=bool(self.cfg["ui"].get("check_updates", True)))
        self.update_interval_days = tk.IntVar(value=int(self.cfg["ui"].get("update_interval_days", 7)))

        self.status_text = tk.StringVar(value=t("status_ready"))
        self.power_state = tk.StringVar(value="")

        self.update_status_text = tk.StringVar(value=f"{t('current_version')}: {APP_VERSION}")
        self.update_detail_text = tk.StringVar(value=t("update_checking"))
        self.pending_release = None

        self.tray = None
        self.power = None

        self._update_check_running = False
        self._update_download_running = False
        self._update_timer_job = None
        self._next_update_check_ts = 0.0
        self._dashboard_render_token = 0

        self._build_ui()
        self._apply_theme()
        self.root.update_idletasks()
        self._apply_titlebar_theme()
        self.refresh_displays()
        self._start_tray()
        self._start_power()
        self._reschedule_update_checks(initial=True)

        self._start_hidden = bool(self.start_minimized.get()) or IS_AUTOSTART
        if not self._start_hidden:
            self.root.deiconify()
            self._fade_in()

        self.root.after(250, self.apply_now)

    def _asset_path(self, name: str) -> Optional[Path]:
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / name)
        candidates.append(Path(__file__).resolve().parent / name)
        candidates.append(Path.cwd() / name)
        for path in candidates:
            if path.exists():
                return path
        return None

    def _set_window_icon(self):
        icon_path = self._asset_path("logo.ico")
        if not icon_path:
            return
        try:
            self.root.iconbitmap(str(icon_path))
        except Exception:
            pass

    def _apply_titlebar_theme(self):
        if not dwmapi:
            return

        try:
            hwnd = self.root.winfo_id()
        except Exception:
            return

        dark_enabled = self.theme_name == "dark"

        try:
            dark_value = ctypes.c_int(1 if dark_enabled else 0)
            for attr in (20, 19):
                try:
                    dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_int(attr),
                        ctypes.byref(dark_value),
                        ctypes.sizeof(dark_value),
                    )
                    break
                except Exception:
                    continue
        except Exception:
            pass

        if dark_enabled:
            try:
                caption_color = ctypes.c_int(0x00000000)
                text_color = ctypes.c_int(0x00FFFFFF)
                for attr, value in ((35, caption_color), (36, text_color)):
                    try:
                        dwmapi.DwmSetWindowAttribute(
                            ctypes.c_void_p(hwnd),
                            ctypes.c_int(attr),
                            ctypes.byref(value),
                            ctypes.sizeof(value),
                        )
                    except Exception:
                        pass
            except Exception:
                pass

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self.root, style="ScreenSwitcher.TNotebook")
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.general_tab = tk.Frame(self.notebook, bd=0, highlightthickness=0)
        self.settings_tab = tk.Frame(self.notebook, bd=0, highlightthickness=0)

        self.notebook.add(self.general_tab, text=t("general_tab"))
        self.notebook.add(self.settings_tab, text=t("settings_tab"))

        self._build_general_tab()
        self._build_settings_tab()

    def _add_button_fx(self, btn: tk.Button, base: Optional[str] = None, hover: Optional[str] = None):
        theme = self._theme()
        base = base or theme["button"]
        hover = hover or theme["panel2"]

        def set_bg(color: str):
            try:
                btn.configure(bg=color, activebackground=color)
            except tk.TclError:
                pass

        def to_rgb(h: str):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

        def to_hex(rgb):
            return "#%02x%02x%02x" % rgb

        def tween(start_hex: str, end_hex: str, steps: int = 4, delay: int = 14):
            s = to_rgb(start_hex)
            e = to_rgb(end_hex)

            def step(i: int = 0):
                r = round(s[0] + (e[0] - s[0]) * (i + 1) / steps)
                g = round(s[1] + (e[1] - s[1]) * (i + 1) / steps)
                b = round(s[2] + (e[2] - s[2]) * (i + 1) / steps)
                set_bg(to_hex((r, g, b)))
                if i + 1 < steps:
                    btn.after(delay, lambda: step(i + 1))

            step(0)

        btn.bind("<Enter>", lambda _e: tween(base, hover))
        btn.bind("<Leave>", lambda _e: tween(self._widget_base_button_color(btn), base))
        btn.bind("<ButtonPress-1>", lambda _e: tween(hover, theme["accent2"], steps=2, delay=10))
        btn.bind("<ButtonRelease-1>", lambda _e: tween(theme["accent2"], hover, steps=2, delay=10))

    def _widget_base_button_color(self, btn: tk.Button) -> str:
        try:
            return btn.cget("bg")
        except Exception:
            return self._theme()["button"]

    def _build_general_tab(self):
        self.general_tab.columnconfigure(0, weight=1)
        self.general_tab.rowconfigure(2, weight=1)

        header = tk.Frame(self.general_tab, bd=0)
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        header.columnconfigure(0, weight=1)

        self.title_label = tk.Label(header, text=t("app_title"), font=("Segoe UI", 18, "bold"), anchor="w")
        self.title_label.grid(row=0, column=0, sticky="w")

        self.subtitle_label = tk.Label(header, text=t("subtitle"), font=("Segoe UI", 9), anchor="w")
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.power_label = tk.Label(header, text="", font=("Segoe UI", 10, "bold"), anchor="w")
        self.power_label.grid(row=2, column=0, sticky="w", pady=(8, 0))

        controls = tk.Frame(self.general_tab, bd=0)
        controls.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        left = tk.Frame(controls, bd=0)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(controls, bd=0)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self.display_title = tk.Label(left, text=t("display_section"), font=("Segoe UI", 11, "bold"), anchor="w")
        self.display_title.pack(anchor="w")

        self.display_combo = ttk.Combobox(
            left,
            textvariable=self.selected_display_label,
            state="readonly",
            width=42,
            style="ScreenSwitcher.TCombobox",
        )
        self.display_combo.pack(fill="x", pady=(6, 6))
        self.display_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_selected_display())
        self.display_combo.configure(postcommand=lambda: self._style_all_combobox_popups())

        self.display_info_label = tk.Label(
            left,
            text=t("display_info"),
            font=("Segoe UI", 9),
            justify="left",
            anchor="w",
            wraplength=390,
        )
        self.display_info_label.pack(anchor="w", fill="x", pady=(0, 8))

        self.display_enabled_check = tk.Checkbutton(left, text=t("monitor_enabled"), variable=self.display_enabled)
        self.display_enabled_check.pack(anchor="w", pady=(0, 8))

        mode_grid = tk.Frame(left, bd=0)
        mode_grid.pack(fill="x", pady=(2, 6))
        mode_grid.columnconfigure(0, weight=1)
        mode_grid.columnconfigure(1, weight=1)

        ac_block = tk.Frame(mode_grid, bd=0)
        ac_block.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        bat_block = tk.Frame(mode_grid, bd=0)
        bat_block.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.ac_label = tk.Label(ac_block, text=t("ac_mode"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.ac_label.pack(anchor="w")
        self.ac_combo = ttk.Combobox(
            ac_block,
            textvariable=self.ac_mode,
            state="readonly",
            style="ScreenSwitcher.TCombobox",
        )
        self.ac_combo.pack(fill="x", pady=(4, 0))
        self.ac_combo.configure(postcommand=lambda: self._style_all_combobox_popups())
        self.ac_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_tray_icon())

        self.battery_label = tk.Label(bat_block, text=t("battery_mode"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.battery_label.pack(anchor="w")
        self.battery_combo = ttk.Combobox(
            bat_block,
            textvariable=self.battery_mode,
            state="readonly",
            style="ScreenSwitcher.TCombobox",
        )
        self.battery_combo.pack(fill="x", pady=(4, 0))
        self.battery_combo.configure(postcommand=lambda: self._style_all_combobox_popups())
        self.battery_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_tray_icon())

        buttons = tk.Frame(left, bd=0)
        buttons.pack(anchor="w", pady=(10, 0))

        self.use_ac_button = tk.Button(buttons, text=t("use_current_ac"), command=lambda: self.use_current_as("ac"), width=16)
        self.use_ac_button.grid(row=0, column=0, padx=(0, 6))
        self._add_button_fx(self.use_ac_button)

        self.use_battery_button = tk.Button(buttons, text=t("use_current_battery"), command=lambda: self.use_current_as("battery"), width=16)
        self.use_battery_button.grid(row=0, column=1, padx=(0, 6))
        self._add_button_fx(self.use_battery_button)

        self.save_display_button = tk.Button(buttons, text=t("save_display"), command=self.save_display_settings, width=14)
        self.save_display_button.grid(row=0, column=2)
        self._add_button_fx(self.save_display_button)

        self.apply_button = tk.Button(right, text=t("apply_now"), command=self.apply_now, width=16)
        self.apply_button.pack(anchor="e")
        self._add_button_fx(self.apply_button)

        self.refresh_button = tk.Button(right, text=t("refresh_modes"), command=self.refresh_displays, width=16)
        self.refresh_button.pack(anchor="e", pady=(8, 0))
        self._add_button_fx(self.refresh_button)

        self.status_label = tk.Label(right, textvariable=self.status_text, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=330)
        self.status_label.pack(anchor="w", fill="x", pady=(18, 0))

        self.cards_title = tk.Label(self.general_tab, text=t("dashboard_title"), font=("Segoe UI", 11, "bold"), anchor="w")
        self.cards_title.grid(row=2, column=0, sticky="ew", padx=14, pady=(4, 0))

        self.cards_container = tk.Frame(self.general_tab, bd=0)
        self.cards_container.grid(row=3, column=0, sticky="nsew", padx=14, pady=(8, 14))
        self.cards_container.rowconfigure(0, weight=1)
        self.cards_container.columnconfigure(0, weight=1)

        self.cards_canvas = tk.Canvas(self.cards_container, bd=0, highlightthickness=0)
        self.cards_scrollbar = ttk.Scrollbar(
            self.cards_container,
            orient="vertical",
            command=self.cards_canvas.yview,
            style="ScreenSwitcher.Vertical.TScrollbar",
        )
        self.cards_canvas.configure(yscrollcommand=self.cards_scrollbar.set)

        self.cards_canvas.grid(row=0, column=0, sticky="nsew")
        self.cards_scrollbar.grid(row=0, column=1, sticky="ns")

        self.cards_area = tk.Frame(self.cards_canvas, bd=0)
        self.cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_area, anchor="nw")

        self.cards_area.bind("<Configure>", self._on_cards_frame_configure)
        self.cards_canvas.bind("<Configure>", self._on_cards_canvas_configure)
        self.cards_canvas.bind("<Enter>", lambda _e: self.cards_canvas.bind_all("<MouseWheel>", self._on_mousewheel))
        self.cards_canvas.bind("<Leave>", lambda _e: self.cards_canvas.unbind_all("<MouseWheel>"))

    def _build_settings_tab(self):
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.columnconfigure(1, weight=1)

        left = tk.Frame(self.settings_tab, bd=0)
        left.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        right = tk.Frame(self.settings_tab, bd=0)
        right.grid(row=0, column=1, sticky="nsew", padx=14, pady=14)

        self.settings_title = tk.Label(left, text=t("settings_title"), font=("Segoe UI", 13, "bold"), anchor="w")
        self.settings_title.pack(anchor="w")

        theme_row = tk.Frame(left, bd=0)
        theme_row.pack(fill="x", pady=(12, 8))
        self.theme_label = tk.Label(theme_row, text=t("theme_label"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.theme_label.pack(anchor="w")
        self.theme_combo = ttk.Combobox(theme_row, textvariable=self.theme_var, state="readonly", style="ScreenSwitcher.TCombobox")
        self.theme_combo.pack(fill="x", pady=(4, 0))
        self.theme_combo.configure(postcommand=lambda: self._style_all_combobox_popups())

        lang_row = tk.Frame(left, bd=0)
        lang_row.pack(fill="x", pady=(4, 8))
        self.language_label = tk.Label(lang_row, text=t("language_label"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.language_label.pack(anchor="w")
        self.language_combo = ttk.Combobox(lang_row, textvariable=self.language_var, state="readonly", style="ScreenSwitcher.TCombobox")
        self.language_combo.pack(fill="x", pady=(4, 0))
        self.language_combo.configure(postcommand=lambda: self._style_all_combobox_popups())

        self.check_updates_check = tk.Checkbutton(right, text=t("check_updates"), variable=self.check_updates_enabled, command=self._sync_update_controls)
        self.check_updates_check.pack(anchor="w", pady=(4, 4))

        interval_row = tk.Frame(right, bd=0)
        interval_row.pack(fill="x", pady=(4, 8))
        self.update_interval_label = tk.Label(interval_row, text=t("update_interval_label"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.update_interval_label.pack(anchor="w")
        self.update_interval_combo = ttk.Combobox(interval_row, state="readonly", style="ScreenSwitcher.TCombobox")
        self.update_interval_combo["values"] = ["1", "3", "7", "14", "30"]
        self.update_interval_combo.pack(fill="x", pady=(4, 0))
        self.update_interval_combo.configure(postcommand=lambda: self._style_all_combobox_popups())

        self.autostart_check = tk.Checkbutton(right, text=t("autostart"), variable=self.autostart)
        self.autostart_check.pack(anchor="w", pady=(8, 4))
        self.start_minimized_check = tk.Checkbutton(right, text=t("start_minimized"), variable=self.start_minimized)
        self.start_minimized_check.pack(anchor="w", pady=4)
        self.tray_check = tk.Checkbutton(right, text=t("minimize_tray"), variable=self.minimize_to_tray)
        self.tray_check.pack(anchor="w", pady=4)

        self.save_settings_button = tk.Button(right, text=t("save_settings"), command=self.save_settings, width=16)
        self.save_settings_button.pack(anchor="w", pady=(12, 0))
        self._add_button_fx(self.save_settings_button)

        self.settings_hint = tk.Label(right, text=t("settings_hint"), font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.settings_hint.pack(anchor="w", fill="x", pady=(16, 0))

        self.settings_status = tk.Label(right, textvariable=self.status_text, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.settings_status.pack(anchor="w", fill="x", pady=(10, 0))

        self.updates_title = tk.Label(right, text=t("updates_title"), font=("Segoe UI", 11, "bold"), anchor="w")
        self.updates_title.pack(anchor="w", pady=(16, 0))

        self.current_version_label = tk.Label(right, textvariable=self.update_status_text, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.current_version_label.pack(anchor="w", fill="x", pady=(6, 0))

        self.update_detail_label = tk.Label(right, textvariable=self.update_detail_text, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.update_detail_label.pack(anchor="w", fill="x", pady=(6, 0))

        update_buttons = tk.Frame(right, bd=0)
        update_buttons.pack(anchor="w", pady=(10, 0))

        self.check_updates_button = tk.Button(update_buttons, text=t("check_updates"), command=self.check_updates_now, width=16)
        self.check_updates_button.grid(row=0, column=0, padx=(0, 6))
        self._add_button_fx(self.check_updates_button)

        self.install_update_button = tk.Button(update_buttons, text=t("update_now"), command=self.install_update, width=16, state="disabled")
        self.install_update_button.grid(row=0, column=1)
        self._add_button_fx(self.install_update_button)

        self._sync_interval_combo()

    def _theme(self) -> dict:
        return self.themes.get(self.theme_name, self.themes["light"])

    def _load_icon_font(self, size: int) -> ImageFont.FreeTypeFont:
        candidates = [
            r"C:\Windows\Fonts\segoeuiz.ttf",
            r"C:\Windows\Fonts\seguisbi.ttf",
            r"C:\Windows\Fonts\segoeuii.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\seguisb.ttf",
            r"C:\Windows\Fonts\arialbi.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ]
        for path in candidates:
            try:
                if os.path.exists(path):
                    return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    def _tray_active_hz(self) -> int:
        dev = self._selected_display()
        if not dev and self.displays:
            dev = self.displays[0]["name"]
        if not dev:
            return 0
        current = get_current_mode(dev)
        return int(current[2]) if current and len(current) >= 3 else 0

    def _tray_icon(self):
        hz = self._tray_active_hz()
        text = str(hz if hz > 0 else 0)
        color = hz_color(hz)

        img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        size = 80 if len(text) <= 2 else 68 if len(text) == 3 else 58
        font = self._load_icon_font(size)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (128 - tw) // 2
        y = (128 - th) // 2 - 4

        draw.text(
            (x, y),
            text,
            fill=color,
            font=font,
            stroke_width=3,
            stroke_fill=(0, 0, 0, 180),
        )
        return img

    def _update_tray_icon(self):
        if not self.tray:
            return
        try:
            self.tray.icon = self._tray_icon()
        except Exception:
            pass

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(t("tray_open"), lambda _icon, _item: self.root.after(0, self.show)),
            pystray.MenuItem(t("tray_apply_ac"), lambda _icon, _item: self.root.after(0, self.apply_ac_only)),
            pystray.MenuItem(t("tray_apply_battery"), lambda _icon, _item: self.root.after(0, self.apply_battery_only)),
            pystray.MenuItem(t("tray_exit"), lambda _icon, _item: self.root.after(0, self.exit)),
        )
        self.tray = pystray.Icon(APP_NAME, self._tray_icon(), APP_NAME, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _restart_tray(self):
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self._start_tray()

    def _start_power(self):
        self.power = PowerWatcher(lambda _state: self.root.after(0, self.apply_now))
        self.power.start()

    def _fade_in(self):
        try:
            self.root.attributes("-alpha", 0.0)
        except Exception:
            return

        steps = 12
        delay = 15

        def step(i: int = 0):
            alpha = min(1.0, (i + 1) / steps)
            try:
                self.root.attributes("-alpha", alpha)
            except Exception:
                return
            if i + 1 < steps:
                self.root.after(delay, lambda: step(i + 1))

        step(0)

    def _sync_interval_combo(self):
        values = ["1", "3", "7", "14", "30"]
        self.update_interval_combo["values"] = values
        current = str(int(self.update_interval_days.get() or 7))
        if current not in values:
            current = "7"
        self.update_interval_combo.set(current)
        self.update_interval_combo.configure(state="readonly" if self.check_updates_enabled.get() else "disabled")

    def _sync_update_controls(self):
        if self.check_updates_enabled.get():
            self.update_interval_combo.configure(state="readonly")
            self.update_detail_text.set(t("update_checking"))
        else:
            self.update_interval_combo.configure(state="disabled")
            self.pending_release = None
            self.install_update_button.configure(state="disabled")
            self.update_detail_text.set(t("update_disabled"))
        self._reschedule_update_checks(initial=False)

    def _cancel_update_timer(self):
        if self._update_timer_job is not None:
            try:
                self.root.after_cancel(self._update_timer_job)
            except Exception:
                pass
            self._update_timer_job = None

    def _reschedule_update_checks(self, initial: bool = False):
        self._cancel_update_timer()

        if not self.check_updates_enabled.get():
            self._next_update_check_ts = 0.0
            return

        if initial:
            self.root.after(8000, self._start_update_check)
        else:
            self.root.after(0, self._start_update_check)

        self._update_timer_job = self.root.after(60 * 60 * 1000, self._update_timer_tick)

    def _update_timer_tick(self):
        self._update_timer_job = None

        if not self.check_updates_enabled.get():
            return

        now = time.time()
        if not self._update_check_running and (self._next_update_check_ts == 0.0 or now >= self._next_update_check_ts):
            self._start_update_check()

        self._update_timer_job = self.root.after(60 * 60 * 1000, self._update_timer_tick)

    def _start_update_check(self):
        if self._update_check_running or not self.check_updates_enabled.get():
            return
        self._update_check_running = True
        self.update_detail_text.set(t("update_checking"))
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _schedule_next_update_due(self):
        if not self.check_updates_enabled.get():
            self._next_update_check_ts = 0.0
            return

        try:
            days = max(1, int(self.update_interval_days.get()))
        except Exception:
            days = 7
        self._next_update_check_ts = time.time() + (days * 86400)

    def _update_worker(self):
        try:
            release = github_request(GITHUB_LATEST_RELEASE_URL, timeout=10)
            latest_tag = str(release.get("tag_name", "")).strip()

            if latest_tag and is_newer_version(latest_tag, APP_VERSION):
                self.root.after(0, lambda rel=release: self._on_update_available(rel))
            else:
                self.root.after(0, self._on_update_none)
        except Exception:
            self.root.after(0, self._on_update_error)
        finally:
            self._update_check_running = False

    def _pick_exe_asset(self, release: dict) -> Optional[dict]:
        assets = release.get("assets") or []
        candidates = [a for a in assets if isinstance(a, dict) and str(a.get("browser_download_url", "")).lower().endswith(".exe")]
        if not candidates:
            return None

        def score(asset: dict) -> tuple:
            name = str(asset.get("name", "")).lower()
            priority = 0
            if "setup" in name or "install" in name:
                priority += 20
            if name.endswith(".exe"):
                priority += 10
            if "main" in name:
                priority += 1
            return priority, len(name)

        candidates.sort(key=score, reverse=True)
        return candidates[0]

    def _on_update_available(self, release: dict):
        self.pending_release = release
        asset = self._pick_exe_asset(release)
        latest_tag = str(release.get("tag_name", "")).strip() or "?"
        if asset:
            self.update_detail_text.set(t("update_ready").format(name=asset.get("name", latest_tag)))
            self.install_update_button.configure(state="normal")
        else:
            self.update_detail_text.set(t("update_missing_asset"))
            self.install_update_button.configure(state="disabled")
        self.update_status_text.set(f"{t('current_version')}: {APP_VERSION}  •  {t('update_available').format(version=latest_tag)}")
        self._schedule_next_update_due()

    def _on_update_none(self):
        self.pending_release = None
        self.install_update_button.configure(state="disabled")
        self.update_status_text.set(f"{t('current_version')}: {APP_VERSION}")
        self.update_detail_text.set(t("update_none"))
        self._schedule_next_update_due()

    def _on_update_error(self):
        self.pending_release = None
        self.install_update_button.configure(state="disabled")
        self.update_status_text.set(f"{t('current_version')}: {APP_VERSION}")
        self.update_detail_text.set(t("update_error"))
        self._schedule_next_update_due()

    def check_updates_now(self):
        if self.check_updates_enabled.get():
            self.update_detail_text.set(t("update_checking"))
        self.root.after(0, self._start_update_check)

    def install_update(self):
        if self._update_download_running:
            return

        release = self.pending_release
        if not release:
            self.update_detail_text.set(t("update_error"))
            return

        asset = self._pick_exe_asset(release)
        if not asset:
            self.update_detail_text.set(t("update_missing_asset"))
            return

        url = asset.get("browser_download_url")
        name = asset.get("name", "ScreenSwitcher-setup.exe")
        if not url:
            self.update_detail_text.set(t("update_error"))
            return

        self._update_download_running = True
        self.install_update_button.configure(state="disabled")
        self.update_detail_text.set(t("update_downloading"))

        target_dir = Path(tempfile.gettempdir()) / "ScreenSwitcher"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / name

        threading.Thread(target=self._download_and_launch_update, args=(url, target_path), daemon=True).start()

    def _download_and_launch_update(self, url: str, target_path: Path):
        try:
            download_url(url, target_path)
            self.root.after(0, lambda: self._launch_update_file(target_path))
        except Exception:
            self.root.after(0, self._on_update_error)
        finally:
            self._update_download_running = False

    def _launch_update_file(self, path: Path):
        self.update_detail_text.set(t("update_launching"))
        try:
            subprocess.Popen([str(path)], cwd=str(path.parent))
        except Exception:
            try:
                os.startfile(str(path))
            except Exception:
                self.update_detail_text.set(t("update_error"))
                self.install_update_button.configure(state="normal")
                return
        self.exit()

    def _style_combobox_popup(self, combo: ttk.Combobox):
        theme = self._theme()
        try:
            combo.update_idletasks()
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", str(combo))
            listbox = self.root.nametowidget(f"{popdown}.f.l")
            listbox.configure(
                bg=theme["bg2"],
                fg=theme["text"],
                selectbackground=theme["panel2"],
                selectforeground=theme["text"],
                highlightthickness=0,
                relief="flat",
                activestyle="none",
                font=("Segoe UI", 10),
            )
            try:
                self.root.nametowidget(popdown).configure(bg=theme["bg2"])
            except Exception:
                pass
        except Exception:
            pass

    def _style_all_combobox_popups(self):
        for combo in (
            getattr(self, "display_combo", None),
            getattr(self, "ac_combo", None),
            getattr(self, "battery_combo", None),
            getattr(self, "theme_combo", None),
            getattr(self, "language_combo", None),
            getattr(self, "update_interval_combo", None),
        ):
            if combo is not None:
                self._style_combobox_popup(combo)

    def _apply_style(self):
        theme = self._theme()
        self.style.configure("ScreenSwitcher.TNotebook", background=theme["bg"], borderwidth=0)
        self.style.configure(
            "ScreenSwitcher.TNotebook.Tab",
            background=theme["panel2"],
            foreground=theme["muted"],
            padding=(12, 6),
            borderwidth=0,
        )
        self.style.map(
            "ScreenSwitcher.TNotebook.Tab",
            background=[("selected", theme["panel"])],
            foreground=[("selected", theme["text"])],
        )

        self.style.configure(
            "ScreenSwitcher.TCombobox",
            fieldbackground=theme["bg2"],
            background=theme["bg2"],
            foreground=theme["text"],
            arrowcolor=theme["text"],
            bordercolor=theme["border"],
            lightcolor=theme["border"],
            darkcolor=theme["border"],
            selectbackground=theme["panel2"],
            selectforeground=theme["text"],
            padding=4,
        )
        self.style.map(
            "ScreenSwitcher.TCombobox",
            fieldbackground=[("readonly", theme["bg2"]), ("disabled", theme["panel2"])],
            foreground=[("readonly", theme["text"]), ("disabled", theme["muted"])],
            background=[("readonly", theme["bg2"])],
        )

        self.style.configure(
            "ScreenSwitcher.Vertical.TScrollbar",
            background=theme["panel2"],
            troughcolor=theme["bg"],
            bordercolor=theme["border"],
            arrowcolor=theme["text"],
            lightcolor=theme["border"],
            darkcolor=theme["border"],
        )

        try:
            self.root.option_add("*TCombobox*Listbox.background", theme["bg2"])
            self.root.option_add("*TCombobox*Listbox.foreground", theme["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", theme["panel2"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", theme["text"])
            self.root.option_add("*Listbox.background", theme["bg2"])
            self.root.option_add("*Listbox.foreground", theme["text"])
            self.root.option_add("*Listbox.selectBackground", theme["panel2"])
            self.root.option_add("*Listbox.selectForeground", theme["text"])
        except Exception:
            pass

        self._style_all_combobox_popups()

    def _walk_widgets(self, widget):
        yield widget
        for child in widget.winfo_children():
            yield from self._walk_widgets(child)

    def _apply_widget_theme(self, widget):
        theme = self._theme()

        if isinstance(widget, (tk.Tk, tk.Frame)):
            widget.configure(bg=theme["bg"])
        elif isinstance(widget, tk.Label):
            try:
                widget.configure(bg=theme["bg"], fg=theme["text"])
            except tk.TclError:
                pass
        elif isinstance(widget, tk.Button):
            try:
                widget.configure(
                    bg=theme["button"],
                    fg=theme["text"],
                    activebackground=theme["panel2"],
                    activeforeground=theme["text"],
                    disabledforeground=theme["muted"],
                    relief="flat",
                    overrelief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    highlightbackground=theme["border"],
                    highlightcolor=theme["border"],
                    padx=10,
                    pady=4,
                    cursor="hand2",
                )
            except tk.TclError:
                pass
        elif isinstance(widget, tk.Checkbutton):
            try:
                widget.configure(
                    bg=theme["bg"],
                    fg=theme["text"],
                    activebackground=theme["bg"],
                    activeforeground=theme["text"],
                    selectcolor=theme["panel"],
                    highlightthickness=0,
                    anchor="w",
                )
            except tk.TclError:
                pass
        elif isinstance(widget, tk.Canvas):
            try:
                widget.configure(bg=theme["bg"])
            except tk.TclError:
                pass

    def _apply_theme(self):
        self.theme = self._theme()
        self.root.configure(bg=self.theme["bg"])
        self._apply_style()

        for widget in self._walk_widgets(self.root):
            self._apply_widget_theme(widget)

        try:
            self.cards_canvas.configure(bg=self.theme["bg"], highlightthickness=0)
            self.cards_container.configure(bg=self.theme["bg"])
            self.cards_area.configure(bg=self.theme["bg"])
        except Exception:
            pass

        self._update_texts()
        self.refresh_dashboard()
        self.root.after(100, self._apply_titlebar_theme)

    def _update_texts(self):
        self.root.title(t("app_title"))
        try:
            self.notebook.tab(0, text=t("general_tab"))
            self.notebook.tab(1, text=t("settings_tab"))
        except Exception:
            pass

        self.title_label.config(text=t("app_title"))
        self.subtitle_label.config(text=t("subtitle"))
        self.display_title.config(text=t("display_section"))
        self.display_info_label.config(text=t("display_info"))
        self.ac_label.config(text=t("ac_mode"))
        self.battery_label.config(text=t("battery_mode"))
        self.use_ac_button.config(text=t("use_current_ac"))
        self.use_battery_button.config(text=t("use_current_battery"))
        self.save_display_button.config(text=t("save_display"))
        self.apply_button.config(text=t("apply_now"))
        self.refresh_button.config(text=t("refresh_modes"))
        self.cards_title.config(text=t("dashboard_title"))

        self.settings_title.config(text=t("settings_title"))
        self.theme_label.config(text=t("theme_label"))
        self.language_label.config(text=t("language_label"))
        self.check_updates_check.config(text=t("check_updates"))
        self.update_interval_label.config(text=t("update_interval_label"))
        self.autostart_check.config(text=t("autostart"))
        self.start_minimized_check.config(text=t("start_minimized"))
        self.tray_check.config(text=t("minimize_tray"))
        self.save_settings_button.config(text=t("save_settings"))
        self.settings_hint.config(text=t("settings_hint"))

        self.updates_title.config(text=t("updates_title"))
        self.current_version_label.config(textvariable=self.update_status_text)
        self.update_detail_label.config(textvariable=self.update_detail_text)
        self.check_updates_button.config(text=t("check_updates"))
        self.install_update_button.config(text=t("update_now"))

        self.theme_combo["values"] = [t("theme_dark"), t("theme_light")]
        self.language_combo["values"] = [t("language_es"), t("language_en")]

        self._theme_reverse = {t("theme_dark"): "dark", t("theme_light"): "light"}
        self._lang_reverse = {t("language_es"): "es", t("language_en"): "en"}

        self.theme_combo.set(t("theme_dark") if self.theme_name == "dark" else t("theme_light"))
        self.language_combo.set(t("language_es") if self.language == "es" else t("language_en"))

        self._sync_interval_combo()
        self._sync_update_controls()
        self._style_all_combobox_popups()

        if self.power_state.get():
            self.power_label.config(text=t("current_mode") + f": {self.power_state.get()}")

    def _on_cards_frame_configure(self, _event=None):
        try:
            self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox("all"))
        except Exception:
            pass

    def _on_cards_canvas_configure(self, event):
        try:
            self.cards_canvas.itemconfigure(self.cards_window, width=event.width)
        except Exception:
            pass

    def _on_mousewheel(self, event):
        try:
            self.cards_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def refresh_displays(self):
        prev_label = self.selected_display_label.get()

        self.displays = enumerate_displays()
        self.display_modes.clear()
        self.display_lookup.clear()

        labels = []
        for display in self.displays:
            dev = display["name"]
            label = f'{display.get("friendly", dev)} — {dev}'
            labels.append(label)
            self.display_lookup[label] = dev
            self.display_modes[dev] = enumerate_modes(dev)

        self.display_combo["values"] = labels

        if labels:
            if prev_label in labels:
                self.selected_display_label.set(prev_label)
            elif self.cfg.get("last_display"):
                found = None
                for label, dev in self.display_lookup.items():
                    if dev == self.cfg["last_display"]:
                        found = label
                        break
                self.selected_display_label.set(found or labels[0])
            else:
                self.selected_display_label.set(labels[0])

            self.load_selected_display()
        else:
            self.selected_display_label.set("")
            self.ac_combo["values"] = []
            self.battery_combo["values"] = []
            self.ac_mode.set("")
            self.battery_mode.set("")
            self.display_enabled.set(False)

        self._sync_controls_state()
        self.refresh_dashboard()
        self._update_tray_icon()

    def _sync_controls_state(self):
        has_displays = bool(self.displays)
        state = "readonly" if has_displays else "disabled"
        self.display_combo.configure(state=state)
        self.ac_combo.configure(state=state)
        self.battery_combo.configure(state=state)
        self.display_enabled_check.configure(state="normal" if has_displays else "disabled")
        self.use_ac_button.configure(state="normal" if has_displays else "disabled")
        self.use_battery_button.configure(state="normal" if has_displays else "disabled")
        self.save_display_button.configure(state="normal" if has_displays else "disabled")
        self.apply_button.configure(state="normal" if has_displays else "disabled")

    def load_selected_display(self):
        label = self.selected_display_label.get()
        dev = self.display_lookup.get(label)

        if not dev:
            self.display_enabled.set(False)
            self.ac_combo["values"] = []
            self.battery_combo["values"] = []
            self.ac_mode.set("")
            self.battery_mode.set("")
            self._update_tray_icon()
            return

        display_cfg = self.cfg.setdefault("displays", {}).get(dev, {})
        self.display_enabled.set(bool(display_cfg.get("enabled", True)))

        modes = self.display_modes.get(dev, [])
        labels = [mode_label(m) for m in modes]
        self.ac_combo["values"] = labels
        self.battery_combo["values"] = labels

        current = get_current_mode(dev)
        default = mode_label(current) if current in modes else (mode_label(modes[0]) if modes else "")

        ac = safe_mode_tuple(display_cfg.get("ac"))
        bat = safe_mode_tuple(display_cfg.get("battery"))

        self.ac_mode.set(mode_label(ac) if ac in modes else default)
        self.battery_mode.set(mode_label(bat) if bat in modes else default)
        self._style_all_combobox_popups()
        self._update_tray_icon()

    def _selected_display(self) -> Optional[str]:
        label = self.selected_display_label.get()
        return self.display_lookup.get(label)

    def use_current_as(self, target: str):
        dev = self._selected_display()
        if not dev:
            return
        current = get_current_mode(dev)
        if not current:
            return
        current_label = mode_label(current)
        if target == "ac":
            self.ac_mode.set(current_label)
        elif target == "battery":
            self.battery_mode.set(current_label)
        self._update_tray_icon()

    def _label_to_mode(self, text: str, modes: List[Tuple[int, int, int]]) -> Optional[Tuple[int, int, int]]:
        try:
            clean = text.replace("x", " ").replace("@", " ").replace("Hz", " ")
            nums = [int(p) for p in clean.split() if p.strip().lstrip("-").isdigit()]
            if len(nums) >= 3:
                candidate = (nums[0], nums[1], nums[2])
                if candidate in modes:
                    return candidate
        except Exception:
            pass
        for mode in modes:
            if mode_label(mode) == text:
                return mode
        return None

    def save_display_settings(self):
        dev = self._selected_display()
        if not dev:
            self.status_text.set(t("status_no_modes"))
            return

        modes = self.display_modes.get(dev, [])
        if not modes:
            self.status_text.set(t("status_no_modes"))
            return

        ac = self._label_to_mode(self.ac_mode.get(), modes)
        battery = self._label_to_mode(self.battery_mode.get(), modes)

        if ac is None:
            ac = modes[0]
        if battery is None:
            battery = modes[0]

        self.cfg.setdefault("displays", {})[dev] = {
            "enabled": bool(self.display_enabled.get()),
            "ac": list(ac),
            "battery": list(battery),
        }
        self.cfg["last_display"] = dev
        save_config(self.cfg)
        self.status_text.set(t("status_saved"))
        self.refresh_dashboard()
        self._update_tray_icon()

    def save_settings(self):
        theme_name = self._theme_reverse.get(self.theme_combo.get(), "light")
        language = self._lang_reverse.get(self.language_combo.get(), "es")

        self.theme_name = theme_name
        self.language = language
        set_language(language)

        try:
            interval = int(self.update_interval_combo.get())
        except Exception:
            interval = 7

        self.cfg["ui"]["theme"] = theme_name
        self.cfg["ui"]["language"] = language
        self.cfg["ui"]["autostart"] = bool(self.autostart.get())
        self.cfg["ui"]["start_minimized"] = bool(self.start_minimized.get())
        self.cfg["ui"]["minimize_to_tray"] = bool(self.minimize_to_tray.get())
        self.cfg["ui"]["check_updates"] = bool(self.check_updates_enabled.get())
        self.cfg["ui"]["update_interval_days"] = interval
        save_config(self.cfg)

        set_autostart(bool(self.autostart.get()))

        self._apply_theme()
        self._restart_tray()
        self._reschedule_update_checks(initial=False)
        self.status_text.set(t("status_saved"))

    def refresh_dashboard(self):
        self._dashboard_render_token += 1
        token = self._dashboard_render_token

        for child in self.cards_area.winfo_children():
            child.destroy()

        current_ac = is_on_ac()
        current_state = t("ac_mode") if current_ac else t("battery_mode")
        self.power_state.set(current_state)
        self.power_label.config(text=t("current_mode") + f": {current_state}")

        items = self.displays if self.displays else enumerate_displays()

        if not items:
            empty = tk.Frame(
                self.cards_area,
                bg=self.theme["panel"],
                highlightthickness=1,
                highlightbackground=self.theme["border"],
                padx=12,
                pady=12,
            )
            empty.pack(fill="x")
            tk.Label(
                empty,
                text=t("status_no_modes"),
                font=("Segoe UI", 12, "italic"),
                bg=self.theme["panel"],
                fg=self.theme["muted"],
            ).pack(anchor="w")
            self._on_cards_frame_configure()
            self._update_tray_icon()
            return

        def render_card(index: int):
            if token != self._dashboard_render_token:
                return

            if index >= len(items):
                self._on_cards_frame_configure()
                self._update_tray_icon()
                return

            display = items[index]
            dev = display["name"]
            friendly = display.get("friendly", dev)
            current = get_current_mode(dev)
            hz = current[2] if current else None
            resolution = f"{current[0]} × {current[1]}" if current else "-- × --"

            cfg = self.cfg.get("displays", {}).get(dev, {})
            enabled = bool(cfg.get("enabled", True))
            ac = safe_mode_tuple(cfg.get("ac"))
            battery = safe_mode_tuple(cfg.get("battery"))

            card = tk.Frame(
                self.cards_area,
                bg=self.theme["panel"],
                highlightthickness=1,
                highlightbackground=self.theme["border"],
                padx=12,
                pady=10,
            )
            card.pack(fill="x", pady=6)

            top_row = tk.Frame(card, bg=self.theme["panel"])
            top_row.pack(fill="x")

            title_text = friendly if enabled else f"{friendly}  ({t('disabled_label')})"
            tk.Label(
                top_row,
                text=title_text,
                font=("Segoe UI", 11, "bold"),
                bg=self.theme["panel"],
                fg=self.theme["text"],
                anchor="w",
            ).pack(side="left", anchor="w")

            tk.Label(
                top_row,
                text=dev,
                font=("Segoe UI", 8),
                bg=self.theme["panel"],
                fg=self.theme["muted"],
                anchor="e",
            ).pack(side="right", anchor="e")

            tk.Label(
                card,
                text=f"{hz} Hz" if hz else "-- Hz",
                font=("Segoe UI", 30, "italic", "bold"),
                bg=self.theme["panel"],
                fg=hz_color(hz),
                anchor="w",
            ).pack(fill="x", pady=(2, 0))

            tk.Label(
                card,
                text=resolution,
                font=("Segoe UI", 9),
                bg=self.theme["panel"],
                fg=self.theme["muted"],
                anchor="w",
            ).pack(fill="x")

            target_text = f"AC: {mode_label(ac) if ac else '--'} | {t('battery_mode')}: {mode_label(battery) if battery else '--'}"
            if not ac and not battery:
                target_text = t("no_target_label")

            tk.Label(
                card,
                text=target_text,
                font=("Segoe UI", 9),
                bg=self.theme["panel"],
                fg=self.theme["muted"],
                anchor="w",
            ).pack(fill="x", pady=(6, 0))

            card.bind("<Enter>", lambda _e, c=card: c.configure(highlightbackground=self.theme["accent"]))
            card.bind("<Leave>", lambda _e, c=card: c.configure(highlightbackground=self.theme["border"]))

            self.cards_area.after(28, lambda: render_card(index + 1))

        render_card(0)

    def apply_now(self):
        current_ac = is_on_ac()
        self.power_state.set(t("ac_mode") if current_ac else t("battery_mode"))
        self.power_label.config(text=t("current_mode") + f": {self.power_state.get()}")

        any_applied = False
        failures = []

        for display in self.displays or enumerate_displays():
            dev = display["name"]
            cfg = self.cfg.get("displays", {}).get(dev)
            if not cfg or not bool(cfg.get("enabled", True)):
                continue

            mode = safe_mode_tuple(cfg.get("ac")) if current_ac else safe_mode_tuple(cfg.get("battery"))
            if not mode:
                continue

            current_mode = get_current_mode(dev)
            if current_mode == mode:
                any_applied = True
                continue

            any_applied = True
            if not apply_mode(dev, mode[0], mode[1], mode[2]):
                failures.append(display.get("friendly", dev))

        self.refresh_dashboard()
        self._update_tray_icon()

        if not any_applied:
            self.status_text.set(t("status_no_modes"))
        elif failures:
            self.status_text.set(f"{t('status_applied')} {', '.join(failures)}")
        else:
            self.status_text.set(t("status_applied"))

    def apply_ac_only(self):
        self._apply_for_state(True)

    def apply_battery_only(self):
        self._apply_for_state(False)

    def _apply_for_state(self, ac_state: bool):
        failures = []
        any_applied = False

        for display in self.displays or enumerate_displays():
            dev = display["name"]
            cfg = self.cfg.get("displays", {}).get(dev)
            if not cfg or not bool(cfg.get("enabled", True)):
                continue

            mode = safe_mode_tuple(cfg.get("ac")) if ac_state else safe_mode_tuple(cfg.get("battery"))
            if not mode:
                continue

            current_mode = get_current_mode(dev)
            if current_mode == mode:
                any_applied = True
                continue

            any_applied = True
            if not apply_mode(dev, mode[0], mode[1], mode[2]):
                failures.append(display.get("friendly", dev))

        self.refresh_dashboard()
        self._update_tray_icon()
        if not any_applied:
            self.status_text.set(t("status_no_modes"))
        elif failures:
            self.status_text.set(f"{t('status_applied')} {', '.join(failures)}")
        else:
            self.status_text.set(t("status_applied"))

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._fade_in()
        self.root.after(50, self._apply_titlebar_theme)

    def _cleanup(self):
        try:
            if self.power:
                self.power.stop()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            if self._mutex:
                kernel32.ReleaseMutex(self._mutex)
                kernel32.CloseHandle(self._mutex)
                self._mutex = None
        except Exception:
            pass

    def exit(self):
        self._cleanup()
        self.root.destroy()

    def on_close(self):
        if self.minimize_to_tray.get():
            self.root.withdraw()
        else:
            self.exit()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()


if __name__ == "__main__":
    App().run()