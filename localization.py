import ctypes


def detect_language() -> str:
    langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    primary = langid & 0xFF
    return "es" if primary == 0x0A else "en"


LANG = detect_language()

STR = {
    "es": {
        "app_title": "ScreenSwitcher",
        "subtitle": "Cambio de perfil Enchufado / batería",
        "general_tab": "General",
        "settings_tab": "Ajustes",
        "display_section": "Pantalla activa",
        "dashboard_title": "Monitores",
        "current_mode": "Modo actual",
        "mode_label": "Modo",
        "available_modes": "Modos disponibles",
        "monitor": "Pantalla",
        "monitor_enabled": "Afectar esta pantalla",
        "ac_mode": "Modo enchufado",
        "battery_mode": "Con batería",
        "settings_title": "Configuración",
        "theme_label": "Tema",
        "language_label": "Idioma",
        "theme_dark": "Oscuro",
        "theme_light": "Claro",
        "language_es": "Español",
        "language_en": "English",
        "autostart": "Iniciar con Windows",
        "start_minimized": "Iniciar minimizado",
        "minimize_tray": "Minimizar a la bandeja al cerrar",
        "save_settings": "Guardar ajustes",
        "apply_now": "Aplicar ahora",
        "refresh_modes": "Actualizar monitores",
        "use_current_ac": "Usar modo actual como enchufado",
        "use_current_battery": "Usar modo actual como batería",
        "save_display": "Guardar",
        "status_ready": "Listo.",
        "status_saved": "Guardado.",
        "status_applied": "Cambios aplicados.",
        "status_no_modes": "No se encontraron modos compatibles.",
        "tray_open": "Abrir",
        "tray_apply_ac": "Aplicar enchufado",
        "tray_apply_battery": "Aplicar batería",
        "tray_exit": "Salir",
        "profile_info": "Selecciona una pantalla y define el Hz para cuando está enchufado y batería.",
        "display_info": "Ajusta el Hz de cada monitor sin llenar la interfaz de pasos extra.",
        "available_modes_hint": "Los modos los detecta Windows para la pantalla seleccionada.",
        "settings_hint": "El tema y el idioma se aplican al guardar.",
        "current_power": "Estado de energía",
        "disabled_label": "desactivada",
        "no_target_label": "Sin objetivo configurado.",
    },
    "en": {
        "app_title": "ScreenSwitcher",
        "subtitle": "AC / battery profile switcher",
        "general_tab": "General",
        "settings_tab": "Settings",
        "display_section": "Active display",
        "dashboard_title": "Monitors",
        "current_mode": "Current mode",
        "mode_label": "Mode",
        "available_modes": "Available modes",
        "monitor": "Display",
        "monitor_enabled": "Manage this display",
        "ac_mode": "AC MODE",
        "battery_mode": "BATTERY MODE",
        "settings_title": "Settings",
        "theme_label": "Theme",
        "language_label": "Language",
        "theme_dark": "Dark",
        "theme_light": "Light",
        "language_es": "Español",
        "language_en": "English",
        "autostart": "Start with Windows",
        "start_minimized": "Start minimized",
        "minimize_tray": "Minimize to tray on close",
        "save_settings": "Save settings",
        "apply_now": "Apply now",
        "refresh_modes": "Refresh modes",
        "use_current_ac": "Use current as AC",
        "use_current_battery": "Use current as battery",
        "save_display": "Save display",
        "status_ready": "Ready.",
        "status_saved": "Saved.",
        "status_applied": "Changes applied.",
        "status_no_modes": "No compatible modes found.",
        "tray_open": "Open",
        "tray_apply_ac": "Apply AC",
        "tray_apply_battery": "Apply battery",
        "tray_exit": "Exit",
        "profile_info": "Select a display and define the Hz for AC and battery.",
        "display_info": "Adjust each monitor's Hz without adding extra screens.",
        "available_modes_hint": "Windows detects the modes for the selected display.",
        "settings_hint": "Theme and language are applied when you save.",
        "current_power": "Power state",
        "disabled_label": "disabled",
        "no_target_label": "No target configured.",
    },
}


def set_language(lang: str) -> None:
    global LANG
    LANG = lang if lang in ("es", "en") else "en"


def t(key: str) -> str:
    return STR[LANG].get(key, key)


def theme_definitions():
    return {
        "dark": {
            "bg": "#000000",
            "bg2": "#050505",
            "panel": "#070707",
            "panel2": "#101010",
            "border": "#242424",
            "text": "#f3f4f6",
            "muted": "#9ca3af",
            "accent": "#22d3ee",
            "accent2": "#a855f7",
            "warn": "#f59e0b",
            "button": "#111111",
            "listbg": "#050505",
        },
        "light": {
            "bg": "#f5f7fb",
            "bg2": "#ffffff",
            "panel": "#ffffff",
            "panel2": "#edf2f7",
            "border": "#d0d7e2",
            "text": "#0f172a",
            "muted": "#475569",
            "accent": "#0f766e",
            "accent2": "#2563eb",
            "warn": "#d97706",
            "button": "#e2e8f0",
            "listbg": "#ffffff",
        },
    }