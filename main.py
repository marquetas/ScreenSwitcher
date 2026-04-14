import ctypes
import json
import os
import sys
import threading
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
MUTEX_NAME = "Local\\ScreenSwitcherMutex"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

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
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve()
    return f'"{sys.executable}" "{script}"'


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_launch_command())
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

        self.status_text = tk.StringVar(value=t("status_ready"))
        self.power_state = tk.StringVar(value="")

        self.tray = None
        self.power = None

        self._build_ui()
        self._apply_theme()
        self.root.update_idletasks()
        self._apply_titlebar_theme()
        self.refresh_displays()
        self._start_tray()
        self._start_power()

        if self.start_minimized.get():
            self.root.withdraw()

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
        self.ac_combo = ttk.Combobox(ac_block, textvariable=self.ac_mode, state="readonly", style="ScreenSwitcher.TCombobox")
        self.ac_combo.pack(fill="x", pady=(4, 0))

        self.battery_label = tk.Label(bat_block, text=t("battery_mode"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.battery_label.pack(anchor="w")
        self.battery_combo = ttk.Combobox(bat_block, textvariable=self.battery_mode, state="readonly", style="ScreenSwitcher.TCombobox")
        self.battery_combo.pack(fill="x", pady=(4, 0))

        buttons = tk.Frame(left, bd=0)
        buttons.pack(anchor="w", pady=(10, 0))

        self.use_ac_button = tk.Button(buttons, text=t("use_current_ac"), command=lambda: self.use_current_as("ac"), width=16)
        self.use_ac_button.grid(row=0, column=0, padx=(0, 6))

        self.use_battery_button = tk.Button(buttons, text=t("use_current_battery"), command=lambda: self.use_current_as("battery"), width=16)
        self.use_battery_button.grid(row=0, column=1, padx=(0, 6))

        self.save_display_button = tk.Button(buttons, text=t("save_display"), command=self.save_display_settings, width=14)
        self.save_display_button.grid(row=0, column=2)

        self.apply_button = tk.Button(right, text=t("apply_now"), command=self.apply_now, width=16)
        self.apply_button.pack(anchor="e")

        self.refresh_button = tk.Button(right, text=t("refresh_modes"), command=self.refresh_displays, width=16)
        self.refresh_button.pack(anchor="e", pady=(8, 0))

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

        lang_row = tk.Frame(left, bd=0)
        lang_row.pack(fill="x", pady=(4, 8))
        self.language_label = tk.Label(lang_row, text=t("language_label"), font=("Segoe UI", 10, "bold"), anchor="w")
        self.language_label.pack(anchor="w")
        self.language_combo = ttk.Combobox(lang_row, textvariable=self.language_var, state="readonly", style="ScreenSwitcher.TCombobox")
        self.language_combo.pack(fill="x", pady=(4, 0))

        self.autostart_check = tk.Checkbutton(right, text=t("autostart"), variable=self.autostart)
        self.autostart_check.pack(anchor="w", pady=(8, 4))
        self.start_minimized_check = tk.Checkbutton(right, text=t("start_minimized"), variable=self.start_minimized)
        self.start_minimized_check.pack(anchor="w", pady=4)
        self.tray_check = tk.Checkbutton(right, text=t("minimize_tray"), variable=self.minimize_to_tray)
        self.tray_check.pack(anchor="w", pady=4)

        self.save_settings_button = tk.Button(right, text=t("save_settings"), command=self.save_settings, width=16)
        self.save_settings_button.pack(anchor="w", pady=(12, 0))

        self.settings_hint = tk.Label(right, text=t("settings_hint"), font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.settings_hint.pack(anchor="w", fill="x", pady=(16, 0))

        self.settings_status = tk.Label(right, textvariable=self.status_text, font=("Segoe UI", 9), justify="left", anchor="w", wraplength=340)
        self.settings_status.pack(anchor="w", fill="x", pady=(10, 0))

    def _theme(self) -> dict:
        return self.themes.get(self.theme_name, self.themes["light"])

    def _load_icon_font(self, size: int) -> ImageFont.FreeTypeFont:
        candidates = [
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\seguisb.ttf",
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

    def _tray_icon(self):
        theme = self._theme()
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle(
            (6, 6, 58, 58),
            radius=14,
            fill=theme["panel"],
            outline=theme["accent"],
            width=2,
        )
        draw.rounded_rectangle(
            (14, 14, 50, 50),
            radius=10,
            fill=theme["bg2"],
            outline=theme["border"],
            width=1,
        )
        draw.line((19, 41, 28, 32), fill=theme["accent"], width=4)
        draw.line((28, 32, 37, 37), fill=theme["accent2"], width=4)
        draw.line((37, 37, 45, 25), fill=theme["warn"], width=4)

        font = self._load_icon_font(16)
        bbox = draw.textbbox((0, 0), "Hz", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text(((64 - tw) // 2, (64 - th) // 2 - 1), "Hz", fill=theme["text"], font=font)
        return img

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
        except Exception:
            pass

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
                    relief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    padx=10,
                    pady=4,
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

    def _apply_card_style(self):
        theme = self._theme()
        self._card_bg = theme["panel"]
        self._card_border = theme["border"]

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

        self._apply_card_style()
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
        self.autostart_check.config(text=t("autostart"))
        self.start_minimized_check.config(text=t("start_minimized"))
        self.tray_check.config(text=t("minimize_tray"))
        self.save_settings_button.config(text=t("save_settings"))
        self.settings_hint.config(text=t("settings_hint"))

        self.theme_combo["values"] = [t("theme_dark"), t("theme_light")]
        self.language_combo["values"] = [t("language_es"), t("language_en")]

        self._theme_reverse = {t("theme_dark"): "dark", t("theme_light"): "light"}
        self._lang_reverse = {t("language_es"): "es", t("language_en"): "en"}

        self.theme_combo.set(t("theme_dark") if self.theme_name == "dark" else t("theme_light"))
        self.language_combo.set(t("language_es") if self.language == "es" else t("language_en"))

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

    def save_settings(self):
        theme_name = self._theme_reverse.get(self.theme_combo.get(), "light")
        language = self._lang_reverse.get(self.language_combo.get(), "es")

        self.theme_name = theme_name
        self.language = language
        set_language(language)

        self.cfg["ui"]["theme"] = theme_name
        self.cfg["ui"]["language"] = language
        self.cfg["ui"]["autostart"] = bool(self.autostart.get())
        self.cfg["ui"]["start_minimized"] = bool(self.start_minimized.get())
        self.cfg["ui"]["minimize_to_tray"] = bool(self.minimize_to_tray.get())
        save_config(self.cfg)

        set_autostart(bool(self.autostart.get()))

        self._apply_theme()
        self._restart_tray()
        self.status_text.set(t("status_saved"))

    def refresh_dashboard(self):
        for child in self.cards_area.winfo_children():
            child.destroy()

        current_ac = is_on_ac()
        current_state = t("ac_mode") if current_ac else t("battery_mode")
        self.power_state.set(current_state)
        self.power_label.config(text=t("current_mode") + f": {current_state}")

        if not self.displays:
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
            return

        for display in self.displays:
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
            ).pack(fill="x", pady=(0, 0))

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

        self._on_cards_frame_configure()

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