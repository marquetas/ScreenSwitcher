import ctypes
from typing import List, Optional, Tuple

user32 = ctypes.windll.user32

ENUM_CURRENT_SETTINGS = -1
DISP_CHANGE_SUCCESSFUL = 0
CDS_TEST = 0x00000002

DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000


class POINTL(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPosition", POINTL),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_ulong),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004


def _make_devmode() -> DEVMODEW:
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def enumerate_displays() -> List[dict]:
    displays = []
    index = 0

    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        ok = user32.EnumDisplayDevicesW(None, index, ctypes.byref(dd), 0)
        if not ok:
            break

        if dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            displays.append(
                {
                    "name": dd.DeviceName,
                    "friendly": dd.DeviceString.strip() or dd.DeviceName,
                    "primary": bool(dd.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE),
                }
            )
        index += 1

    return displays


def get_current_mode(device_name: Optional[str] = None) -> Optional[Tuple[int, int, int]]:
    dm = _make_devmode()
    if not user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return None

    hz = int(dm.dmDisplayFrequency) if int(dm.dmDisplayFrequency) else 60
    return int(dm.dmPelsWidth), int(dm.dmPelsHeight), hz


def enumerate_modes(device_name: Optional[str] = None) -> List[Tuple[int, int, int]]:
    modes = set()
    index = 0

    while True:
        dm = _make_devmode()
        ok = user32.EnumDisplaySettingsW(device_name, index, ctypes.byref(dm))
        if not ok:
            break

        w = int(dm.dmPelsWidth)
        h = int(dm.dmPelsHeight)
        hz = int(dm.dmDisplayFrequency) if int(dm.dmDisplayFrequency) else 60
        if w > 0 and h > 0 and hz > 0:
            modes.add((w, h, hz))
        index += 1

    return sorted(modes, key=lambda m: (m[0] * m[1], m[0], m[1], m[2]))


def mode_label(mode: Tuple[int, int, int]) -> str:
    w, h, hz = mode
    return f"{w} x {h} @ {hz} Hz"


def parse_mode_label(text: str) -> Optional[Tuple[int, int, int]]:
    try:
        clean = text.replace("x", " ").replace("@", " ").replace("Hz", " ")
        nums = [int(p) for p in clean.split() if p.strip().lstrip("-").isdigit()]
        if len(nums) >= 3:
            return nums[0], nums[1], nums[2]
    except Exception:
        pass
    return None


def apply_mode(device_name: Optional[str], width: int, height: int, hz: int) -> bool:
    dm = _make_devmode()
    if not user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return False

    dm.dmPelsWidth = width
    dm.dmPelsHeight = height
    dm.dmDisplayFrequency = hz
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY

    test = user32.ChangeDisplaySettingsExW(device_name, ctypes.byref(dm), None, CDS_TEST, None)
    if test != DISP_CHANGE_SUCCESSFUL:
        return False

    result = user32.ChangeDisplaySettingsExW(device_name, ctypes.byref(dm), None, 0, None)
    return result == DISP_CHANGE_SUCCESSFUL