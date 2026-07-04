import sys
import math
import json
import threading
import ctypes
import os
import time
import socket
from ctypes import wintypes
from typing import Optional

# 避免 Windows 上 Qt 輸出 DPI awareness 的無害警告
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

from PyQt6 import QtWidgets, QtCore, QtGui
from monitorcontrol import get_monitors
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser
import numpy as np
import dxcam
import wmi

try:
    import winreg
except ImportError:
    winreg = None

SETTINGS_FILE = "settings.json"
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)

# 自動亮度公式係數（可在此統一調整）
# 當前亮度 = (avg * AUTO_BRIGHTNESS_CONTENT_COEFF + backlight * weight) / (AUTO_BRIGHTNESS_CONTENT_COEFF + weight)
AUTO_BRIGHTNESS_CONTENT_COEFF = 1.0
AUTO_BRIGHTNESS_CONTENT_COEFF_MIN_FACTOR = 0.5
AUTO_BRIGHTNESS_CONTENT_COEFF_MAX_FACTOR = 1.5
AUTO_BRIGHTNESS_WEIGHT_DEFAULT = 1.0
NETWORK_DEBUG_LOG_ENABLED = False

MODIFIER_ORDER = ["Alt", "Ctrl", "Shift", "Win"]
SHORTCUT_MODIFIER_OPTIONS = ["None"] + MODIFIER_ORDER
SHORTCUT_KEY_OPTIONS = (
    [f"NumPad{i}" for i in range(10)]
    + ["NumPad."]
    + [str(i) for i in range(10)]
    + [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    + [f"F{i}" for i in range(1, 13)]
    + ["Left", "Up", "Right", "Down", "PageUp", "PageDown", "Home", "End"]
    + ["音量靜音", "音量降低", "音量提高", "媒體上一首", "媒體下一首", "媒體播放", "媒體暫停", "媒體停止"]
    + ["滑鼠左鍵", "滑鼠右鍵", "滑鼠中鍵", "滑鼠上一頁", "滑鼠下一頁"]
)
SHORTCUT_TYPE_OPTIONS = ["絕對值", "+Step", "-Step", "切換自動亮度"]
KEY_NAME_TO_VK = {
    "Alt": 0x12,
    "Ctrl": 0x11,
    "Shift": 0x10,
    "Win": 0x5B,
    **{str(i): 0x30 + i for i in range(10)},
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
    **{f"NumPad{i}": 0x60 + i for i in range(10)},
    "NumPad.": 0x6E,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "PageUp": 0x21,
    "PageDown": 0x22,
    "Home": 0x24,
    "End": 0x23,
    "音量靜音": 0xAD,   # VK_VOLUME_MUTE
    "音量降低": 0xAE,   # VK_VOLUME_DOWN
    "音量提高": 0xAF,   # VK_VOLUME_UP
    "媒體上一首": 0xB1, # VK_MEDIA_PREV_TRACK
    "媒體下一首": 0xB0, # VK_MEDIA_NEXT_TRACK
    "媒體播放": 0xB3,   # VK_MEDIA_PLAY_PAUSE
    "媒體暫停": 0xB3,   # VK_MEDIA_PLAY_PAUSE
    "媒體停止": 0xB2,   # VK_MEDIA_STOP
    "滑鼠左鍵": 0x01,   # VK_LBUTTON
    "滑鼠右鍵": 0x02,   # VK_RBUTTON
    "滑鼠中鍵": 0x04,   # VK_MBUTTON
    "滑鼠上一頁": 0x05, # VK_XBUTTON1
    "滑鼠下一頁": 0x06, # VK_XBUTTON2
}
KEY_NAME_TO_VKS = {
    key: (vk,)
    for key, vk in KEY_NAME_TO_VK.items()
}
KEY_NAME_TO_VKS.update({
    "Alt": (0x12, 0xA4, 0xA5),    # VK_MENU, VK_LMENU, VK_RMENU
    "Ctrl": (0x11, 0xA2, 0xA3),   # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
    "Shift": (0x10, 0xA0, 0xA1),  # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
    "Win": (0x5B, 0x5C),          # VK_LWIN, VK_RWIN
})
DEFAULT_LEVEL_SHORTCUTS = [
    {"keys": ["Ctrl", "NumPad0"], "type": "絕對值", "value": 0},
    {"keys": ["Ctrl", "NumPad1"], "type": "絕對值", "value": 25},
    {"keys": ["Ctrl", "NumPad2"], "type": "絕對值", "value": 50},
    {"keys": ["Ctrl", "NumPad3"], "type": "絕對值", "value": 75},
    {"keys": ["Ctrl", "NumPad."], "type": "絕對值", "value": 100},
]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


# =========================
# 熱插拔監聽器 + 背景偵測 + DDC 逾時包裝
# =========================

class _MonitorHotplugWatcher(QtCore.QObject):
    """背景執行緒監聽 WMI 螢幕熱插拔事件，偵測到變動時通知 UI 執行緒重新掃描。
    若 WMI 不可用（非 Windows / 權限不足），則降級為定時輪詢（timer）模式。"""
    monitors_changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._thread: threading.Thread | None = None
        self._poll_timer: QtCore.QTimer | None = None

    def start(self, poll_interval_ms: int = 5000):
        if self._running:
            return
        self._running = True
        # 先用 WMI 事件監聽（非阻塞背景執行緒）
        if sys.platform == "win32":
            try:
                self._thread = threading.Thread(target=self._wmi_event_loop, daemon=True, name="WMIHotplug")
                self._thread.start()
                return  # WMI 成功啟動，不再需要 timer
            except Exception:
                pass  # WMI 失敗則降級到 timer
        # 降級：用 QTimer 定期輪詢
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self.monitors_changed.emit)
        self._poll_timer.start(poll_interval_ms)

    def stop(self):
        self._running = False
        if self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer = None

    def _wmi_event_loop(self):
        """在背景執行緒監聽 WMI 熱插拔事件（Win32_DesktopMonitor 的建立/刪除）。"""
        try:
            import wmi as wmi_mod
            wmi_conn = wmi_mod.WMI()
            watch_creation = wmi_conn.watch_for(
                notification_type="Creation",
                wmi_class="Win32_DesktopMonitor",
                delay_secs=1,
            )
            watch_deletion = wmi_conn.watch_for(
                notification_type="Deletion",
                wmi_class="Win32_DesktopMonitor",
                delay_secs=1,
            )
            from collections import deque
            watches = deque([watch_creation, watch_deletion])
            while self._running and watches:
                watcher = watches.popleft()
                try:
                    watcher(timeout_ms=2000)
                    if self._running:
                        self.monitors_changed.emit()
                except wmi_mod.x_wmi_timed_out:
                    pass
                except Exception:
                    pass
                if self._running:
                    watches.append(watcher)
        except Exception:
            # WMI 事件監聽失敗（權限不足等），不做任何事（主執行緒會用 timer 降級）
            pass


def _run_ddc_with_timeout(func, timeout_sec: float = 3.0, default=None):
    """在獨立執行緒執行 DDC 操作，若逾時則返回 default 值。避免卡死的螢幕凍結 UI。"""
    result = [default]
    exception = [None]
    event = threading.Event()

    def worker():
        try:
            result[0] = func()
        except Exception as e:
            exception[0] = e
        finally:
            event.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        print(f"[DDC] Timeout ({timeout_sec}s) — monitor may be disconnected")
        return default
    if exception[0]:
        raise exception[0]
    return result[0]


# _MonitorDetectWorker 已移除，統一由 _RefreshDetectWorker + refresh_monitors() 處理。


def _rect_to_tuple(rect):
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def get_windows_display_rects():
    if sys.platform != "win32":
        return {}

    user32 = ctypes.windll.user32
    rects = {}

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def enum_proc(hmonitor, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rects[str(info.szDevice)] = {
                "rect": _rect_to_tuple(info.rcMonitor),
                "primary": bool(info.dwFlags & 1),
            }
        return True

    try:
        user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(enum_proc), 0)
    except Exception:
        return {}
    return rects


def get_dxgi_display_targets():
    """Return DXGI targets ordered like visible monitors: primary first, then position."""
    try:
        factory = getattr(dxcam, "__factory", None)
        if factory is None:
            return []
        metadata = dxcam.get_output_metadata()
        display_rects = get_windows_display_rects()
        targets = []
        for device_idx, outputs in enumerate(getattr(factory, "outputs", [])):
            for output_idx, output in enumerate(outputs):
                display_name = getattr(output, "devicename", "")
                meta = metadata.get(display_name) or []
                rect_info = display_rects.get(display_name, {})
                rect = rect_info.get("rect")
                primary = bool(meta[1]) if len(meta) > 1 else bool(rect_info.get("primary", False))
                targets.append({
                    "device_idx": int(device_idx),
                    "output_idx": int(output_idx),
                    "display_name": display_name,
                    "primary": primary,
                    "rect": rect,
                })

        def sort_key(target):
            rect = target.get("rect") or (10**9, 10**9, 10**9, 10**9)
            return (0 if target.get("primary") else 1, rect[1], rect[0], target["device_idx"], target["output_idx"])

        return sorted(targets, key=sort_key)
    except Exception as e:
        print(f"DXGI output mapping error: {e}")
        return []


def normalize_modifiers(modifiers):
    normalized = []
    for modifier in modifiers:
        if modifier in MODIFIER_ORDER and modifier not in normalized:
            normalized.append(modifier)
    return normalized


def get_dynamic_content_coeff(luminance):
    """依畫面亮度動態調整內容權重。

    畫面越暗，內容權重越高，讓背光補償更多；
    畫面越亮，內容權重越低，讓背光更接近目標亮度。
    """
    try:
        luminance = max(0.0, min(100.0, float(luminance)))
    except (TypeError, ValueError):
        luminance = 50.0

    darkness_ratio = 1.0 - (luminance / 100.0)
    factor = (
        AUTO_BRIGHTNESS_CONTENT_COEFF_MIN_FACTOR
        + darkness_ratio * (AUTO_BRIGHTNESS_CONTENT_COEFF_MAX_FACTOR - AUTO_BRIGHTNESS_CONTENT_COEFF_MIN_FACTOR)
    )
    return float(AUTO_BRIGHTNESS_CONTENT_COEFF) * factor


def _network_log_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def log_network_signal(direction, content, value):
    if not NETWORK_DEBUG_LOG_ENABLED:
        return
    direction = {"發送": "->", "接收": "<-"}.get(direction, direction)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} {direction} {content} {_network_log_value(value)}")


def log_network_payload(direction, payload):
    if not isinstance(payload, dict):
        return
    if payload.get("name") is not None and ("brightness" in payload or "contrast" in payload):
        brightness = payload.get("brightness")
        contrast = payload.get("contrast")
        if contrast is None:
            value = f"brightness={brightness}"
        else:
            value = f"brightness={brightness}, contrast={contrast}"
        log_network_signal(direction, payload.get("name", "unknown"), value)
    if "global_link" in payload:
        log_network_signal(direction, "global", payload.get("global_link"))
    if "auto_enabled" in payload:
        log_network_signal(direction, "自動亮度", bool(payload.get("auto_enabled")))
    if "auto_target" in payload:
        log_network_signal(direction, "自動亮度目標", payload.get("auto_target"))
    for monitor in payload.get("monitors", []) or []:
        if not isinstance(monitor, dict):
            continue
        name = monitor.get("name", "unknown")
        brightness = monitor.get("brightness")
        contrast = monitor.get("contrast")
        if contrast is None:
            value = f"brightness={brightness}"
        else:
            value = f"brightness={brightness}, contrast={contrast}"
        log_network_signal(direction, name, value)


def get_monitor_display_name(monitor, index, caps=None):
    # 嘗試從 VCP capabilities dict 取得 model
    if isinstance(caps, dict):
        model = caps.get("model", "").strip()
        if model and _is_valid_monitor_name(model):
            return model

    # 嘗試從 monitor 物件的各屬性取得名稱
    for attr_name in ("name", "display_name", "description", "model", "monitor_name", "edid"):
        value = getattr(monitor, attr_name, None)
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if _is_valid_monitor_name(cleaned):
                return cleaned

    # 嘗試從 EDID 取得名稱
    try:
        edid = getattr(monitor, "edid", None) or getattr(monitor, "get_edid", lambda: None)()
        if edid and isinstance(edid, bytes):
            name = _parse_edid_monitor_name(edid)
            if name:
                return name
    except Exception:
        pass

    for attr_name in ("manufacturer", "brand"):
        value = getattr(monitor, attr_name, None)
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if _is_valid_monitor_name(cleaned):
                return f"{cleaned} {index + 1}"

    return f"Display {index + 1}"


def _is_valid_monitor_name(name: str) -> bool:
    """檢查字串是否像有效的螢幕名稱（拒絕原始 VCP capabilities 文字）。"""
    if not name or len(name) < 2 or len(name) > 100:
        return False
    # 包含 VCP 原始資料特徵（大量括號、prot()/type()/model() 等）→ 拒絕
    suspicious = ("prot(", "type(", "model(", "vcp(", "cmds(", "mccs_ver")
    if any(s in name.lower() for s in suspicious):
        return False
    # 包含控制字元或換行 → 拒絕
    for ch in name:
        if ord(ch) < 32 or ord(ch) == 127:
            return False
    return True


def _parse_edid_monitor_name(edid: bytes) -> str | None:
    """從 EDID 解析螢幕名稱（Descriptor Block 類型 0xFC 為 Monitor Name）。"""
    try:
        if len(edid) < 128:
            return None
        for offset in range(54, 126, 18):
            tag = edid[offset + 3]
            if tag == 0xFC:
                raw = edid[offset + 5 : offset + 18]
                name = raw.decode("utf-8", errors="replace").strip().rstrip("\n").strip()
                if name and _is_valid_monitor_name(name):
                    return name
    except Exception:
        pass
    return None


def _wmi_brightness_supported():
    try:
        conn = wmi.WMI(namespace="WMI")
        methods = list(conn.WmiMonitorBrightnessMethods())
        monitors = list(conn.WmiMonitorBrightness())
        return bool(methods and monitors)
    except Exception:
        return False


def _wmi_set_brightness(value):
    if not _wmi_brightness_supported():
        return False
    try:
        conn = wmi.WMI(namespace="WMI")
        percent = int(max(0, min(100, value)))
        for method in conn.WmiMonitorBrightnessMethods():
            method.WmiSetBrightness(percent, 0)
        return True
    except Exception:
        return False


def _wmi_get_brightness():
    if not _wmi_brightness_supported():
        return None
    try:
        conn = wmi.WMI(namespace="WMI")
        monitors = list(conn.WmiMonitorBrightness())
        if monitors:
            value = getattr(monitors[0], "CurrentBrightness", None)
            return int(value) if value is not None else None
    except Exception:
        return None


def qt_key_event_to_name(event, allow_modifiers=False):
    key = event.key()
    modifiers = event.modifiers()

    modifier_map = {
        QtCore.Qt.Key.Key_Control: "Ctrl",
        QtCore.Qt.Key.Key_Shift: "Shift",
        QtCore.Qt.Key.Key_Alt: "Alt",
        QtCore.Qt.Key.Key_Meta: "Win",
    }
    if key in modifier_map:
        return modifier_map[key] if allow_modifiers else None

    if key in (QtCore.Qt.Key.Key_Control, QtCore.Qt.Key.Key_Shift, QtCore.Qt.Key.Key_Alt, QtCore.Qt.Key.Key_Meta):
        return None

    keypad_modifier = bool(modifiers & QtCore.Qt.KeyboardModifier.KeypadModifier)

    if QtCore.Qt.Key.Key_0 <= key <= QtCore.Qt.Key.Key_9:
        digit = str(key - QtCore.Qt.Key.Key_0)
        return f"NumPad{digit}" if keypad_modifier else digit

    if QtCore.Qt.Key.Key_A <= key <= QtCore.Qt.Key.Key_Z:
        return chr(key)

    if QtCore.Qt.Key.Key_F1 <= key <= QtCore.Qt.Key.Key_F12:
        return f"F{key - QtCore.Qt.Key.Key_F1 + 1}"

    if key in (QtCore.Qt.Key.Key_Period, QtCore.Qt.Key.Key_Delete) and keypad_modifier:
        return "NumPad."

    arrow_map = {
        QtCore.Qt.Key.Key_Left: "Left",
        QtCore.Qt.Key.Key_Up: "Up",
        QtCore.Qt.Key.Key_Right: "Right",
        QtCore.Qt.Key.Key_Down: "Down",
        QtCore.Qt.Key.Key_PageUp: "PageUp",
        QtCore.Qt.Key.Key_PageDown: "PageDown",
        QtCore.Qt.Key.Key_Home: "Home",
        QtCore.Qt.Key.Key_End: "End",
    }
    if key in arrow_map:
        return arrow_map[key]

    media_map = {
        QtCore.Qt.Key.Key_VolumeMute: "音量靜音",
        QtCore.Qt.Key.Key_VolumeDown: "音量降低",
        QtCore.Qt.Key.Key_VolumeUp: "音量提高",
        QtCore.Qt.Key.Key_MediaPrevious: "媒體上一首",
        QtCore.Qt.Key.Key_MediaNext: "媒體下一首",
        QtCore.Qt.Key.Key_MediaPlay: "媒體播放",
        QtCore.Qt.Key.Key_MediaPause: "媒體暫停",
        QtCore.Qt.Key.Key_MediaStop: "媒體停止",
    }
    if key in media_map:
        return media_map[key]

    return None


def set_slider_object_value(slider_obj, value):
    value = int(round(value))
    slider_obj.slider.blockSignals(True)
    slider_obj.slider.setValue(value)
    slider_obj.slider.blockSignals(False)
    slider_obj.value_label.setText(str(value))


def link_value_from_levels(wrapper, brightness, contrast):
    b_min, b_max = wrapper.brightness_range
    c_min, c_max = wrapper.contrast_range

    if not getattr(wrapper, "contrast_supported", True):
        if brightness is None:
            brightness = b_min
        brightness = max(b_min, min(b_max, int(round(brightness))))
        b_range = max(0, b_max - b_min)
        if b_range <= 0:
            return 0
        return int(round(((brightness - b_min) / b_range) * 100))

    b_range = max(0, b_max - b_min)
    c_range = max(0, c_max - c_min)
    total = b_range + c_range
    if total <= 0:
        return 0

    if brightness is None and contrast is None:
        return 0
    if brightness is None:
        brightness = b_min
    if contrast is None:
        contrast = c_min

    brightness = max(b_min, min(b_max, int(round(brightness))))
    contrast = max(c_min, min(c_max, int(round(contrast))))

    if brightness <= b_min:
        units = max(0, min(c_range, contrast - c_min))
    else:
        units = c_range + max(0, min(b_range, brightness - b_min))

    return int(round((units / total) * 100))


def levels_from_link_value(wrapper, percent, unsupported_contrast: Optional[int] = 0):
    percent = max(0.0, min(100.0, float(percent)))
    b_min, b_max = wrapper.brightness_range
    c_min, c_max = wrapper.contrast_range

    if not getattr(wrapper, "contrast_supported", True):
        b_range = max(0, b_max - b_min)
        brightness = b_min if b_range <= 0 else b_min + (percent / 100.0) * b_range
        return int(round(max(b_min, min(b_max, brightness)))), unsupported_contrast

    b_range = max(0, b_max - b_min)
    c_range = max(0, c_max - c_min)
    total = b_range + c_range
    if total <= 0:
        return int(round(b_min)), int(round(c_min))

    value = percent / 100.0 * total
    if value <= c_range:
        return int(round(b_min)), int(round(c_min + value))
    return int(round(b_min + (value - c_range))), int(round(c_max))


def levels_from_link_units(wrapper, units):
    b_min, b_max = wrapper.brightness_range
    c_min, c_max = wrapper.contrast_range
    b_range = max(0, b_max - b_min)
    contrast_supported = getattr(wrapper, "contrast_supported", True)
    c_range = max(0, c_max - c_min) if contrast_supported else 0
    total = b_range + c_range
    units = max(0, min(total, int(round(units))))

    if total <= 0:
        return int(round(b_min)), 0 if not contrast_supported else int(round(c_min))
    if not contrast_supported:
        return int(round(b_min + units)), 0
    if units <= c_range:
        return int(round(b_min)), int(round(c_min + units))
    return int(round(b_min + (units - c_range))), int(round(c_max))


# =========================
# Windows Global Hook (Alt + Win + Wheel)
# =========================
class GlobalHotkeyWheelHook(QtCore.QObject):
    step_requested = QtCore.pyqtSignal(int)
    level_requested = QtCore.pyqtSignal(int)
    toggle_auto_requested = QtCore.pyqtSignal()

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14

    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_MOUSEWHEEL = 0x020A
    WM_LBUTTONDOWN = 0x0201
    WM_RBUTTONDOWN = 0x0204
    WM_MBUTTONDOWN = 0x0207
    WM_XBUTTONDOWN = 0x020B

    VK_MENU = 0x12
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    WPARAM_T = ctypes.c_size_t
    LPARAM_T = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_uint32),
            ("scanCode", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

        # 明確設定 WinAPI 簽章，避免 64-bit 參數被錯誤轉型
        self.user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, self.WPARAM_T, self.LPARAM_T]
        self.user32.CallNextHookEx.restype = self.LRESULT

        self.user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p

        self.user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self.user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.restype = wintypes.BOOL

        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostThreadMessageW.restype = wintypes.BOOL

        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short

        self.trigger_keys = ["Alt", "Win", "None"]
        self.level_shortcuts = []

        self.alt_down = False
        self.win_down = False
        self.running = False
        self.thread = None
        self.thread_id = 0

        self.keyboard_hook = None
        self.mouse_hook = None
        self._keyboard_proc = None
        self._mouse_proc = None

    def set_trigger_shortcut(self, *keys):
        normalized = []
        for key in keys:
            key_name = key if key in KEY_NAME_TO_VKS else "None"
            if key_name != "None" and key_name not in normalized:
                normalized.append(key_name)
        self.trigger_keys = (normalized + ["None", "None", "None"])[:3]

    def set_level_shortcuts(self, shortcuts):
        normalized_shortcuts = []
        for shortcut in shortcuts:
            shortcut_keys = shortcut.get("keys")
            if isinstance(shortcut_keys, list):
                keys = [key for key in shortcut_keys[:3] if key in KEY_NAME_TO_VKS]
            else:
                modifiers = normalize_modifiers(shortcut.get("modifiers", []))
                key_name = shortcut.get("key")
                keys = [key for key in modifiers + [key_name] if key in KEY_NAME_TO_VKS]
            if not keys:
                continue

            sc_type = shortcut.get("type", "絕對值")
            value = int(max(0, min(100, shortcut.get("value", 0))))
            normalized_shortcuts.append({
                "keys": tuple(keys[:3]),
                "type": sc_type,
                "value": value,
            })

        self.level_shortcuts = normalized_shortcuts

    def _is_modifier_pressed(self, key_name):
        if key_name in (None, "", "None"):
            return True
        if key_name == "Alt":
            return bool(self.user32.GetAsyncKeyState(self.VK_MENU) & 0x8000)
        if key_name == "Ctrl":
            return bool(self.user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
        if key_name == "Shift":
            return bool(self.user32.GetAsyncKeyState(self.VK_SHIFT) & 0x8000)
        if key_name == "Win":
            return bool(self.user32.GetAsyncKeyState(self.VK_LWIN) & 0x8000) or bool(self.user32.GetAsyncKeyState(self.VK_RWIN) & 0x8000)
        return False

    def _is_key_pressed(self, key_name):
        if key_name in (None, "", "None"):
            return True
        vks = KEY_NAME_TO_VKS.get(key_name)
        if not vks:
            return False
        return any(bool(self.user32.GetAsyncKeyState(vk) & 0x8000) for vk in vks)

    def _key_name_matches_vk(self, key_name, vk):
        return int(vk) in KEY_NAME_TO_VKS.get(key_name, ())

    def _active_keys_pressed(self, keys, current_vk=None):
        active_keys = [key_name for key_name in keys if key_name not in (None, "", "None")]
        if not active_keys:
            return False
        for key_name in active_keys:
            if current_vk is not None and self._key_name_matches_vk(key_name, current_vk):
                continue
            if not self._is_key_pressed(key_name):
                return False
        return True

    def _match_level_shortcut(self, vk):
        for shortcut in self.level_shortcuts:
            keys = shortcut["keys"]
            if not any(self._key_name_matches_vk(key_name, vk) for key_name in keys):
                continue
            if self._active_keys_pressed(keys, current_vk=vk):
                return shortcut["type"], shortcut["value"]
        return None

    def start(self):
        if sys.platform != "win32" or self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_hook_loop, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self.thread:
            self.thread.join(timeout=1.0)

    def _run_hook_loop(self):
        self.thread_id = self.kernel32.GetCurrentThreadId()

        HOOKPROC = ctypes.WINFUNCTYPE(self.LRESULT, ctypes.c_int, self.WPARAM_T, self.LPARAM_T)

        def keyboard_proc(nCode, wParam, lParam):
            if nCode >= 0:
                kbd = ctypes.cast(lParam, ctypes.POINTER(self.KBDLLHOOKSTRUCT)).contents
                vk = kbd.vkCode
                msg = int(wParam)

                if msg in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                    match = self._match_level_shortcut(vk)
                    if match is not None:
                        sc_type, value = match
                        if sc_type == "+Step":
                            self.step_requested.emit(1)
                        elif sc_type == "-Step":
                            self.step_requested.emit(-1)
                        elif sc_type == "切換自動亮度":
                            self.toggle_auto_requested.emit()
                        else:
                            self.level_requested.emit(value)
                        return 1

            return self.user32.CallNextHookEx(0, nCode, wParam, lParam)

        def mouse_proc(nCode, wParam, lParam):
            if nCode >= 0:
                msg = int(wParam)

                mouse_vk = {
                    self.WM_LBUTTONDOWN: 0x01,   # VK_LBUTTON
                    self.WM_RBUTTONDOWN: 0x02,   # VK_RBUTTON
                    self.WM_MBUTTONDOWN: 0x04,   # VK_MBUTTON
                }.get(msg)
                if mouse_vk is None and msg == self.WM_XBUTTONDOWN:
                    ms = ctypes.cast(lParam, ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
                    xbtn = (ms.mouseData >> 16) & 0xFFFF
                    mouse_vk = {0x0001: 0x05, 0x0002: 0x06}.get(xbtn)

                if mouse_vk is not None:
                    match = self._match_level_shortcut(mouse_vk)
                    if match is not None:
                        sc_type, value = match
                        if sc_type == "+Step":
                            self.step_requested.emit(1)
                        elif sc_type == "-Step":
                            self.step_requested.emit(-1)
                        elif sc_type == "切換自動亮度":
                            self.toggle_auto_requested.emit()
                        else:
                            self.level_requested.emit(value)
                        return 1

                if msg == self.WM_MOUSEWHEEL:
                    ms = ctypes.cast(lParam, ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
                    high_word = (ms.mouseData >> 16) & 0xFFFF
                    delta = ctypes.c_short(high_word).value

                    if self._active_keys_pressed(self.trigger_keys):
                        wheel_steps = int(delta / 120) if delta != 0 else 0
                        if wheel_steps == 0:
                            wheel_steps = 1 if delta > 0 else -1
                        self.step_requested.emit(wheel_steps)
                        return 1
            return self.user32.CallNextHookEx(0, nCode, wParam, lParam)

        self._keyboard_proc = HOOKPROC(keyboard_proc)
        self._mouse_proc = HOOKPROC(mouse_proc)

        self.keyboard_hook = self.user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._keyboard_proc, None, 0)
        self.mouse_hook = self.user32.SetWindowsHookExW(self.WH_MOUSE_LL, self._mouse_proc, None, 0)
        if not self.keyboard_hook:
            print(f"Keyboard hook install failed: {ctypes.get_last_error()}")
        if not self.mouse_hook:
            print(f"Mouse hook install failed: {ctypes.get_last_error()}")

        msg = wintypes.MSG()
        while self.running and self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

        if self.keyboard_hook:
            self.user32.UnhookWindowsHookEx(self.keyboard_hook)
            self.keyboard_hook = None
        if self.mouse_hook:
            self.user32.UnhookWindowsHookEx(self.mouse_hook)
            self.mouse_hook = None

        self.thread_id = 0
        self.alt_down = False
        self.win_down = False

# =========================
# Worker Thread
# =========================
class DDCWorker(QtCore.QRunnable):
    def __init__(self, monitor, lock, brightness=None, contrast=None, contrast_supported=True):
        super().__init__()
        self.monitor = monitor
        self.lock = lock
        self.brightness = brightness
        self.contrast = contrast
        self.contrast_supported = contrast_supported

    def run(self):
        try:
            # 同一台螢幕序列化送出 DDC 指令，避免 context manager 狀態互相覆蓋
            with self.lock:
                with self.monitor as m:
                    if self.brightness is not None:
                        m.set_luminance(int(self.brightness))
                    if self.contrast_supported and self.contrast is not None:
                        m.set_contrast(int(self.contrast))
            return
        except Exception as e:
            if self.brightness is not None and _wmi_set_brightness(self.brightness):
                return
            print("DDC Error:", e)

# =========================
# Monitor Wrapper
# =========================
class MonitorWrapper:
    def __init__(self, monitor=None, index=0, name="", b_range=None, c_range=None):
        self.monitor = monitor
        self.lock = threading.Lock()
        self.index = index
        self.brightness_range = list(b_range or [0, 100])
        self.contrast_range = list(c_range or [0, 100])
        self.supported = monitor is not None
        self.brightness_supported = monitor is not None
        self.contrast_supported = monitor is not None
        self.available = monitor is not None
        self.wmi_supported = _wmi_brightness_supported()
        self.name = name or f"Display {index + 1}"
        self._cached_brightness: int | None = None
        self._cached_contrast: int | None = None

        if monitor is None:
            # 如果傳入了名稱但無效，使用預設
            if name and not _is_valid_monitor_name(self.name):
                self.name = f"Display {index + 1}"
            return

        caps = None
        try:
            with monitor:
                caps = monitor.get_vcp_capabilities()
                supported_vcp = {}
                if isinstance(caps, dict):
                    supported_vcp = caps.get("vcp", {}) or caps.get("cmds", {})

                if isinstance(supported_vcp, dict):
                    self.brightness_supported = 0x10 in supported_vcp
                    self.contrast_supported = 0x12 in supported_vcp

                try:
                    val = int(monitor.get_luminance())
                    self._cached_brightness = val
                    self.brightness_supported = True
                except Exception:
                    pass
                try:
                    val = int(monitor.get_contrast())
                    self._cached_contrast = val
                    self.contrast_supported = True
                except Exception:
                    pass

                self.supported = self.brightness_supported or self.contrast_supported or self.wmi_supported
                self.available = self.supported
                if self.wmi_supported and not self.contrast_supported:
                    self.contrast_supported = False
        except Exception:
            # DDC 通訊失敗 → 若 WMI 可用則回退到筆電亮度控制；否則標記為不可用
            self.supported = self.wmi_supported
            self.brightness_supported = self.wmi_supported
            self.contrast_supported = False
            self.available = self.wmi_supported
        self.name = get_monitor_display_name(monitor, index, caps)

    def read_current_levels(self):
        if not self.available or self.monitor is None:
            return None, None
        brightness = None
        contrast = None
        try:
            with self.lock:
                def _read():
                    with self.monitor as m:
                        b = None
                        c = None
                        try:
                            b = int(m.get_luminance())
                        except Exception:
                            b = None
                        try:
                            c = int(m.get_contrast())
                        except Exception:
                            c = None
                        return b, c
                brightness, contrast = _run_ddc_with_timeout(
                    _read, timeout_sec=3.0, default=(None, None)
                )
        except Exception:
            pass

        # 若 DDC 讀取失敗，回退到建構時的快取值（跨執行緒場景特別有用）
        if brightness is None:
            brightness = self._cached_brightness
        if contrast is None:
            contrast = self._cached_contrast

        if brightness is None:
            brightness = _wmi_get_brightness()

        if brightness is not None:
            b_min, b_max = self.brightness_range
            brightness = max(b_min, min(b_max, brightness))
        if contrast is None and not self.contrast_supported:
            contrast = 0
        if contrast is not None:
            c_min, c_max = self.contrast_range
            contrast = max(c_min, min(c_max, contrast))

        return brightness, contrast

# =========================
# Monitor UI
# =========================
class MonitorWidget(QtWidgets.QGroupBox):
    value_changed = QtCore.pyqtSignal(int)

    def __init__(self, monitor_wrapper, threadpool):
        if isinstance(monitor_wrapper, RemoteMonitorWrapper):
            host = getattr(monitor_wrapper, "_server_hostname", "")
            display_title = f"{host}: {monitor_wrapper.name}"
        else:
            display_title = monitor_wrapper.name
        super().__init__(display_title)

        self.monitor = monitor_wrapper
        self.threadpool = threadpool

        self.debounce_timer = QtCore.QTimer()
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.timeout.connect(self.apply_values)

        self.pending_brightness = None
        self.pending_contrast = None

        # 螢幕名稱字體設為白色
        self.setStyleSheet("QGroupBox::title { color: white; }")

        layout = QtWidgets.QVBoxLayout()
        # 讓版面更加緊湊
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)

        # ===== Sliders =====
        self.b_slider = self.create_slider("Brightness")
        self.c_slider = self.create_slider("Contrast")
        self.link_slider = self.create_slider("Link Value")

        self.b_slider.slider.valueChanged.connect(self.on_brightness)
        self.c_slider.slider.valueChanged.connect(self.on_contrast)
        self.link_slider.slider.valueChanged.connect(self.on_link)

        layout.addWidget(self.b_slider.widget)
        layout.addWidget(self.c_slider.widget)
        layout.addWidget(self.link_slider.widget)
        self.auto_info_label = QtWidgets.QLabel("畫面亮度: -- | 背光亮度: -- | 加權亮度: -- | 目標亮度: -- | 權重: -- | 來源: --")
        self.auto_info_label.setWordWrap(True)
        self.auto_info_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.auto_info_label)

        self.setLayout(layout)
        self.set_ranges(self.monitor.brightness_range, self.monitor.contrast_range)

    def create_slider(self, name):
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0) # 緊湊設定

        label = QtWidgets.QLabel(name)
        label.setMinimumWidth(60)
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        value_label = QtWidgets.QLabel("0")
        value_label.setMinimumWidth(25)

        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))

        layout.addWidget(label)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        container.setLayout(layout)

        return type("SliderObj", (), {"widget": container, "slider": slider, "value_label": value_label})

    def set_ranges(self, brightness_range, contrast_range):
        b_min, b_max = brightness_range
        c_min, c_max = contrast_range

        self.monitor.brightness_range = [b_min, b_max]
        self.monitor.contrast_range = [c_min, c_max]

        self.b_slider.slider.setRange(b_min, b_max)
        self.c_slider.slider.setRange(c_min, c_max)

        b_val = max(b_min, min(b_max, self.b_slider.slider.value()))
        c_val = max(c_min, min(c_max, self.c_slider.slider.value()))
        self.b_slider.slider.setValue(b_val)
        self.c_slider.slider.setValue(c_val)
        self.b_slider.value_label.setText(str(b_val))
        self.c_slider.value_label.setText(str(c_val))

    def _sync_link_value_from_current_levels(self):
        brightness = self.pending_brightness if self.pending_brightness is not None else self.b_slider.slider.value()
        contrast = self.pending_contrast if self.pending_contrast is not None else self.c_slider.slider.value()
        link_value = max(0, min(100, link_value_from_levels(self.monitor, brightness, contrast)))
        set_slider_object_value(self.link_slider, link_value)
        return link_value

    def on_brightness(self, v):
        self.pending_brightness = v
        link_value = self._sync_link_value_from_current_levels()
        self.value_changed.emit(link_value)
        self.restart()

    def on_contrast(self, v):
        if not self.monitor.contrast_supported:
            self.pending_contrast = 0
            self.sync_sliders(self.b_slider.slider.value(), 0)
            link_value = self._sync_link_value_from_current_levels()
            self.value_changed.emit(link_value)
            return
        self.pending_contrast = v
        link_value = self._sync_link_value_from_current_levels()
        self.value_changed.emit(link_value)
        self.restart()

    def on_link(self, percent):
        if not self.monitor.available:
            return
        brightness, contrast = levels_from_link_value(self.monitor, percent)
        self.pending_brightness = brightness
        self.pending_contrast = contrast
        self.sync_sliders(brightness, contrast)
        set_slider_object_value(self.link_slider, percent)
        self.restart()
        self.value_changed.emit(percent)

    def sync_sliders(self, b, c):
        self.b_slider.slider.blockSignals(True)
        self.c_slider.slider.blockSignals(True)

        b_value = int(round(b))
        c_value = int(round(c))

        self.b_slider.slider.setValue(b_value)
        self.c_slider.slider.setValue(c_value)
        self.b_slider.value_label.setText(str(b_value))
        self.c_slider.value_label.setText(str(c_value))

        self.b_slider.slider.blockSignals(False)
        self.c_slider.slider.blockSignals(False)

    def restart(self):
        self.debounce_timer.start(100)

    def apply_values(self):
        if not self.monitor.available:
            return
        if isinstance(self.monitor, RemoteMonitorWrapper):
            return
        contrast_value = 0 if not self.monitor.contrast_supported else self.pending_contrast
        worker = DDCWorker(
            self.monitor.monitor,
            self.monitor.lock,
            self.pending_brightness,
            contrast_value,
            contrast_supported=self.monitor.contrast_supported,
        )
        self.threadpool.start(worker)

    def set_available(self, available):
        """灰階/恢復顯示，可用時啟用滑桿，不可用時鎖定"""
        opacity = 1.0 if available else 0.4
        self.setStyleSheet(f"QGroupBox::title {{ color: white; }} QGroupBox {{ color: rgba(255,255,255,{opacity}); }}")
        for attr in ("b_slider", "c_slider", "link_slider"):
            obj = getattr(self, attr, None)
            if not obj:
                continue
            enabled = available
            if attr == "c_slider":
                enabled = available and self.monitor.contrast_supported
            obj.slider.setEnabled(enabled)
            if attr == "c_slider" and not enabled:
                obj.slider.setValue(0)
                obj.value_label.setText("0")
        if available:
            self.setTitle(self.monitor.name)
        else:
            self.setTitle(f"{self.monitor.name} (不可用)")

    def set_auto_info(self, text):
        self.auto_info_label.setText(text)


class MonitorRangeWidget(QtWidgets.QGroupBox):
    ranges_changed = QtCore.pyqtSignal(list, list)

    def __init__(self, monitor_wrapper):
        if isinstance(monitor_wrapper, RemoteMonitorWrapper):
            host = getattr(monitor_wrapper, "_server_hostname", "")
            display = f"{host}: {monitor_wrapper.name} Settings"
        else:
            display = f"{monitor_wrapper.name} Settings"
        super().__init__(display)
        self.monitor = monitor_wrapper

        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.b_min = QtWidgets.QSpinBox()
        self.b_max = QtWidgets.QSpinBox()
        self.c_min = QtWidgets.QSpinBox()
        self.c_max = QtWidgets.QSpinBox()

        for widget in [self.b_min, self.b_max, self.c_min, self.c_max]:
            widget.setRange(0, 100)
            widget.valueChanged.connect(self.on_value_changed)

        layout.addWidget(QtWidgets.QLabel("B Min"), 0, 0)
        layout.addWidget(self.b_min, 0, 1)
        layout.addWidget(QtWidgets.QLabel("B Max"), 0, 2)
        layout.addWidget(self.b_max, 0, 3)
        layout.addWidget(QtWidgets.QLabel("C Min"), 1, 0)
        layout.addWidget(self.c_min, 1, 1)
        layout.addWidget(QtWidgets.QLabel("C Max"), 1, 2)
        layout.addWidget(self.c_max, 1, 3)
        self.setLayout(layout)

        self.set_ranges(self.monitor.brightness_range, self.monitor.contrast_range, emit_signal=False)

    def set_ranges(self, brightness_range, contrast_range, emit_signal=True):
        b_min, b_max = brightness_range
        c_min, c_max = contrast_range

        self.b_min.blockSignals(True)
        self.b_max.blockSignals(True)
        self.c_min.blockSignals(True)
        self.c_max.blockSignals(True)

        self.b_min.setValue(b_min)
        self.b_max.setValue(b_max)
        self.c_min.setValue(c_min)
        self.c_max.setValue(c_max)

        self.b_min.blockSignals(False)
        self.b_max.blockSignals(False)
        self.c_min.blockSignals(False)
        self.c_max.blockSignals(False)

        self.monitor.brightness_range = [b_min, b_max]
        self.monitor.contrast_range = [c_min, c_max]
        if emit_signal:
            self.ranges_changed.emit(self.monitor.brightness_range, self.monitor.contrast_range)

    def on_value_changed(self):
        self.set_ranges([self.b_min.value(), self.b_max.value()], [self.c_min.value(), self.c_max.value()])


class ShortcutConfigRow(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    remove_requested = QtCore.pyqtSignal(object)

    def __init__(self, shortcut=None):
        super().__init__()
        shortcut = shortcut or {"keys": ["Ctrl", "NumPad0", "None"], "type": "絕對值", "value": 0}

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.key1_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.key2_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.key3_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(SHORTCUT_TYPE_OPTIONS)
        self.value_label = QtWidgets.QLabel("亮度 %")
        self.value_spin = QtWidgets.QSpinBox()
        self.value_spin.setRange(0, 100)
        self.value_label.setFixedWidth(self.value_label.sizeHint().width())
        self.value_spin.setFixedWidth(max(70, self.value_spin.sizeHint().width()))
        self.remove_button = QtWidgets.QPushButton("刪除")

        layout.addWidget(QtWidgets.QLabel("快捷鍵"))
        layout.addWidget(self.key1_button)
        layout.addWidget(QtWidgets.QLabel("+"))
        layout.addWidget(self.key2_button)
        layout.addWidget(QtWidgets.QLabel("+"))
        layout.addWidget(self.key3_button)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.value_label)
        layout.addWidget(self.value_spin)
        layout.addWidget(self.remove_button)
        self.setLayout(layout)

        self.key1_button.key_changed.connect(self.changed)
        self.key2_button.key_changed.connect(self.changed)
        self.key3_button.key_changed.connect(self.changed)
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        self.value_spin.valueChanged.connect(self.changed)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))

        self.set_data(shortcut)

    def _on_type_changed(self, sc_type):
        is_absolute = (sc_type == "絕對值")
        if is_absolute:
            self.value_label.setStyleSheet("")
            self.value_spin.setStyleSheet("")
            self.value_spin.setEnabled(True)
        else:
            # 僅視覺隱藏，保留版面空間，避免切換時排版跳動
            self.value_label.setStyleSheet("color: transparent;")
            self.value_spin.setStyleSheet(
                "QSpinBox {"
                " color: transparent;"
                " background: transparent;"
                " border: 1px solid transparent;"
                " selection-background-color: transparent;"
                " selection-color: transparent;"
                "}"
                "QSpinBox::up-button, QSpinBox::down-button {"
                " width: 0px;"
                " border: none;"
                " background: transparent;"
                "}"
            )
            self.value_spin.setEnabled(False)
        self.changed.emit()

    def set_data(self, shortcut):
        keys = shortcut.get("keys")
        if isinstance(keys, list):
            keys = list(keys[:3])
        else:
            modifiers = normalize_modifiers(shortcut.get("modifiers", []))
            key = shortcut.get("key", "NumPad0")
            keys = modifiers + [key]
        while len(keys) < 3:
            keys.append("None")
        sc_type = shortcut.get("type", "絕對值")
        value = int(max(0, min(100, shortcut.get("value", 0))))

        for button, key in zip([self.key1_button, self.key2_button, self.key3_button], keys):
            button.set_key_name(key if key in KEY_NAME_TO_VKS or key == "None" else "None")
        self.type_combo.setCurrentText(sc_type if sc_type in SHORTCUT_TYPE_OPTIONS else "絕對值")
        self.value_spin.setValue(value)
        self._on_type_changed(self.type_combo.currentText())

    def get_data(self):
        keys = [
            self.key1_button.key_name,
            self.key2_button.key_name,
            self.key3_button.key_name,
        ]
        keys = [key for key in keys if key in KEY_NAME_TO_VKS]
        sc_type = self.type_combo.currentText()
        return {
            "keys": keys,
            "type": sc_type,
            "value": int(self.value_spin.value()) if sc_type == "絕對值" else 0,
        }


class KeyCaptureButton(QtWidgets.QPushButton):
    key_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, allow_none=False, allow_modifiers=False):
        super().__init__(parent)
        self.allow_none = bool(allow_none)
        self.allow_modifiers = bool(allow_modifiers)
        self.key_name = "NumPad0"
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.clicked.connect(self.begin_capture)
        self._capture_mode = False
        self.update_text()

    def begin_capture(self):
        self._capture_mode = True
        self.setText("請按主鍵...")
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)

    def set_key_name(self, key_name):
        if key_name in KEY_NAME_TO_VKS:
            self.key_name = key_name
        elif key_name == "None" and self.allow_none:
            self.key_name = "None"
        else:
            self.key_name = "None" if self.allow_none else "NumPad0"
        self._capture_mode = False
        self.update_text()
        self.key_changed.emit(self.key_name)

    def update_text(self):
        self.setText("None" if self.key_name == "None" else self.key_name)

    def keyPressEvent(self, event):
        if not self._capture_mode:
            return super().keyPressEvent(event)

        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._capture_mode = False
            self.update_text()
            event.accept()
            return

        key_name = qt_key_event_to_name(event, allow_modifiers=self.allow_modifiers)
        if key_name is not None:
            self.set_key_name(key_name)

        event.accept()

    def mousePressEvent(self, event):
        if not self._capture_mode:
            return super().mousePressEvent(event)

        mouse_btn_map = {
            QtCore.Qt.MouseButton.LeftButton: "滑鼠左鍵",
            QtCore.Qt.MouseButton.RightButton: "滑鼠右鍵",
            QtCore.Qt.MouseButton.MiddleButton: "滑鼠中鍵",
            QtCore.Qt.MouseButton.XButton1: "滑鼠上一頁",
            QtCore.Qt.MouseButton.XButton2: "滑鼠下一頁",
        }
        key_name = mouse_btn_map.get(event.button())
        if key_name is not None:
            self.set_key_name(key_name)
        event.accept()

    def focusOutEvent(self, event):
        if self._capture_mode:
            if self.allow_none:
                self.set_key_name("None")
            else:
                self._capture_mode = False
                self.update_text()
        super().focusOutEvent(event)


class _CaptureThread(QtCore.QThread):
    result_ready = QtCore.pyqtSignal(float, str)  # (亮度 0-100, 來源: "DXGI" / "—")

    _dxgi_cameras = {}
    _dxgi_lock = threading.Lock()
    _dxgi_disabled = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.use_dxgi = True

    @classmethod
    def initialize_dxgi(cls) -> list:
        """初始化 DXGI 工廠並回傳可用 targets（供 _init_screen_analyzers 等共用）。
        若 dxcam 不可用或初始化失敗則回傳 []. 同時重置信號狀態。"""
        cls._dxgi_disabled = False
        try:
            # 確保 factory 被初始化（dxcam.create 會設定 __factory）
            if getattr(dxcam, "__factory", None) is None:
                cls._get_dxgi_camera(0, 0)
            return get_dxgi_display_targets()
        except Exception as e:
            print(f"DXGI init error: {e}")
            cls._dxgi_disabled = True
            return []

    @classmethod
    def reset_dxgi(cls):
        """完全重設 DXGI 狀態：停止 + 釋放所有 camera 並清除快取。"""
        with cls._dxgi_lock:
            for key, cam in list(cls._dxgi_cameras.items()):
                try:
                    if cam.is_capturing:
                        cam.stop()
                    cam.release()
                except Exception:
                    pass
            cls._dxgi_cameras = {}
            cls._dxgi_disabled = False

    @classmethod
    def _get_dxgi_camera(cls, device_idx=0, output_idx=0):
        if cls._dxgi_disabled:
            return None
        key = (int(device_idx), int(output_idx))
        with cls._dxgi_lock:
            if key not in cls._dxgi_cameras:
                cls._dxgi_cameras[key] = dxcam.create(
                    device_idx=key[0],
                    output_idx=key[1],
                    output_color="RGB",
                )
            return cls._dxgi_cameras[key]

    @classmethod
    def _disable_dxgi(cls, device_idx=None, output_idx=None):
        with cls._dxgi_lock:
            if device_idx is None or output_idx is None:
                for key, cam in list(cls._dxgi_cameras.items()):
                    try:
                        if cam.is_capturing:
                            cam.stop()
                        cam.release()
                    except Exception:
                        pass
                cls._dxgi_disabled = True
                cls._dxgi_cameras = {}
            else:
                key = (int(device_idx), int(output_idx))
                cam = cls._dxgi_cameras.pop(key, None)
                if cam is not None:
                    try:
                        if cam.is_capturing:
                            cam.stop()
                        cam.release()
                    except Exception:
                        pass

    def _capture_dxgi(self):
        """使用 DXGI 方式截圖（高效）"""
        device_idx = 0
        output_idx = 0
        try:
            parent = self.parent()
            device_idx = int(getattr(parent, "dxgi_device_idx", 0) or 0)
            output_idx = int(getattr(parent, "dxgi_output_idx", getattr(parent, "output_idx", 0)) or 0)
            camera = self._get_dxgi_camera(device_idx, output_idx)
            if camera is None:
                return None

            # 進行截圖（numpy array）
            frame = camera.grab()
            if frame is None:
                return None

            # 轉為灰度並計算平均值
            # frame 通常是 (height, width, 3) 的 numpy 數組（RGB）
            gray = 0.299 * frame[:, :, 0] + 0.587 * frame[:, :, 1] + 0.114 * frame[:, :, 2]
            # 降採樣以加快處理
            downsampled = gray[::8, ::8]  # 每 8 像素取 1 個
            avg = np.mean(downsampled)
            return avg / 255.0 * 100.0
        except Exception as e:
            print(f"DXGI 截圖錯誤 (device={device_idx}, output={output_idx}): {e}")
            if "0x887A0026" in str(e):
                # 螢幕切換/休眠後輸出變更 → 完全重設 DXGI，下次 tick 可重建 camera
                self.__class__.reset_dxgi()
            else:
                self._disable_dxgi(device_idx, output_idx)
                self.use_dxgi = False
            return None

    def run(self):
        result = None
        source = "—"

        result = self._capture_dxgi()
        if result is not None:
            source = "DXGI"

        if result is not None:
            self.result_ready.emit(result, source)


class ScreenAnalyzer(QtCore.QObject):
    adjust_requested = QtCore.pyqtSignal(float)  # 每 tick 建議調整的百分比（可正可負）
    luminance_updated = QtCore.pyqtSignal(float)  # 即時畫面亮度 0-100
    luminance_source_updated = QtCore.pyqtSignal(str)  # 亮度來源："DXGI" / "—"

    def __init__(self, parent=None, output_idx=0, dxgi_target=None):
        super().__init__(parent)
        self.output_idx = int(output_idx)
        dxgi_target = dxgi_target or {}
        self.dxgi_device_idx = int(dxgi_target.get("device_idx", 0))
        self.dxgi_output_idx = int(dxgi_target.get("output_idx", self.output_idx))
        self.dxgi_display_name = str(dxgi_target.get("display_name", ""))
        self.enabled = False
        self._last_source = "—"
        self.target = 50        # 目標畫面亮度 0-100
        self._k = 0.8           # 平方根曲線係數
        self.weight = AUTO_BRIGHTNESS_WEIGHT_DEFAULT  # 背光權重
        # 調整步階由平方根曲線自動決定
        self.total_levels = 100 # 由 MainWindow 更新
        self.resource_saving_enabled = True
        self.resource_saving_idle_seconds = 5.0
        self._base_capture_interval_seconds = 1.0
        self._current_capture_interval_seconds = 1.0
        self._no_change_elapsed_seconds = 0.0
        self._last_captured_luminance = None
        self._last_luminance = None
        self._current_ddc = 50
        self._current_ddc_float = 50.0
        self._desired_ddc = 50.0
        self._direction = 0     # -1=降低 0=停止 1=提高
        self._capture_thread = None
        self.capture_interval_seconds = 1.0
        self.tick_interval_ms = 200

        # 截圖 timer：依設定間隔判斷方向
        self._capture_timer = QtCore.QTimer(self)
        self._capture_timer.setInterval(int(self.capture_interval_seconds * 1000))
        self._capture_timer.timeout.connect(self._tick_capture)

        # 微調 timer：每次調整最細 1 経 DDC level
        self._adjust_timer = QtCore.QTimer(self)
        self._adjust_timer.setInterval(self.tick_interval_ms)
        self._adjust_timer.timeout.connect(self._tick_adjust)

    def start(self):
        self._capture_timer.start()

    def stop(self):
        self._capture_timer.stop()
        self._adjust_timer.stop()

    def set_current_ddc(self, value, force=False):
        if not force and self._direction != 0:
            return
        self._current_ddc = int(value)
        self._current_ddc_float = float(value)

    def set_capture_interval_seconds(self, seconds):
        seconds = max(0.1, min(5.0, float(seconds)))
        self.capture_interval_seconds = seconds
        self._base_capture_interval_seconds = seconds
        self.reset_dynamic_capture_interval()

    def set_resource_saving(self, enabled, idle_seconds):
        self.resource_saving_enabled = bool(enabled)
        self.resource_saving_idle_seconds = max(0.1, min(60.0, float(idle_seconds)))
        if not self.resource_saving_enabled:
            self.reset_dynamic_capture_interval()

    def set_tick_interval_ms(self, ms):
        ms = max(10, min(2000, int(ms)))
        self.tick_interval_ms = ms
        self._adjust_timer.setInterval(ms)

    def set_k(self, k):
        self._k = max(0.1, min(5.0, float(k)))

    def _auto_threshold(self, c, w):
        """由平方根曲線參數自動推導反應門檻（effective 亮度空間）。
        保留擴充點：未來可加入自適應滯環偵測（方向反覆翻轉時動態放寬門檻）。"""
        min_step = 0.5
        # DDC 空間中可解析的最小步階
        ddc_deadband = (min_step / max(self._k, 0.01)) ** 2
        # 轉換到 effective 空間
        return ddc_deadband * w / (c + w)

    def recalculate_desired_from_last_luminance(self):
        if self._last_luminance is None:
            return
        w = max(0.01, float(self.weight))
        c = get_dynamic_content_coeff(self._last_luminance)
        desired = ((c + w) * self.target - self._last_luminance * c) / w
        self._desired_ddc = max(0.0, min(100.0, desired))
        if self._desired_ddc > self._current_ddc_float:
            self._direction = 1
        elif self._desired_ddc < self._current_ddc_float:
            self._direction = -1
        else:
            self._direction = 0
        if self._direction != 0 and not self._adjust_timer.isActive():
            self._adjust_timer.start()

    def reset_dynamic_capture_interval(self):
        self._current_capture_interval_seconds = self._base_capture_interval_seconds
        self._capture_timer.setInterval(int(round(self._current_capture_interval_seconds * 1000)))
        self._no_change_elapsed_seconds = 0.0
        self._last_captured_luminance = None

    def _tick_capture(self):
        if not self.enabled:
            self._direction = 0
            self._adjust_timer.stop()
            self.reset_dynamic_capture_interval()
            return
        if self._capture_thread is not None and self._capture_thread.isRunning():
            return
        self._capture_thread = _CaptureThread(self)
        self._capture_thread.result_ready.connect(self._on_captured)
        self._capture_thread.start()

    def _on_captured(self, lum, source="—"):
        lum = float(lum)
        self._last_luminance = lum
        self._last_source = source
        self.luminance_source_updated.emit(source)

        if self.resource_saving_enabled:
            if self._last_captured_luminance is not None:
                lum_diff = abs(lum - self._last_captured_luminance)
                if lum_diff < 0.1:
                    self._no_change_elapsed_seconds += self._current_capture_interval_seconds
                    if self._no_change_elapsed_seconds >= self.resource_saving_idle_seconds:
                        next_interval = min(5.0, self._current_capture_interval_seconds * 2.0)
                        if next_interval > self._current_capture_interval_seconds:
                            self._current_capture_interval_seconds = next_interval
                            self._capture_timer.setInterval(int(round(self._current_capture_interval_seconds * 1000)))
                        self._no_change_elapsed_seconds = 0.0
                else:
                    if self._current_capture_interval_seconds != self._base_capture_interval_seconds:
                        self._current_capture_interval_seconds = self._base_capture_interval_seconds
                        self._capture_timer.setInterval(int(round(self._current_capture_interval_seconds * 1000)))
                    self._no_change_elapsed_seconds = 0.0
            self._last_captured_luminance = lum

        self.luminance_updated.emit(lum)

        # 廣播亮度給已連線的 TCP clients（若 server 已啟用）
        try:
            parent = self.parent()
            if parent and getattr(parent, "_network_server_enabled", False) and getattr(parent, "_net_server", None):
                try:
                    parent._net_server.broadcast_luminance(lum, source)
                except Exception:
                    pass
        except Exception:
            pass

        # 使用者指定公式（可調權重）：
        # 當前亮度 = (平均 + 背光*權重) / (2 + 權重 - 1) = (平均 + 背光*權重) / (1 + 權重)
        # 這裡以可調係數泛化：
        # effective = (avg*c + backlight*w) / (c+w)
        w = max(0.01, float(self.weight))
        c = get_dynamic_content_coeff(lum)
        effective = (lum * c + self._current_ddc * w) / (c + w)
        diff = self.target - effective

        if abs(diff) <= self._auto_threshold(c, w):
            self._direction = 0
            self._adjust_timer.stop()
            return

        # 由公式反推所需背光值：
        # target = (avg*c + backlight*w) / (c+w)
        # backlight = ((c+w)*target - avg*c) / w
        desired = ((c + w) * self.target - lum * c) / w
        self._desired_ddc = max(0.0, min(100.0, desired))

        if self._desired_ddc > self._current_ddc_float:
            self._direction = 1
        elif self._desired_ddc < self._current_ddc_float:
            self._direction = -1
        else:
            self._direction = 0

        if self._direction != 0 and not self._adjust_timer.isActive():
            self._adjust_timer.start()

    def _tick_adjust(self):
        if not self.enabled or self._direction == 0:
            self._adjust_timer.stop()
            return

        if self._last_luminance is not None:
            w = max(0.01, float(self.weight))
            c = get_dynamic_content_coeff(self._last_luminance)
            effective = (self._last_luminance * c + self._current_ddc_float * w) / (c + w)
            if abs(self.target - effective) <= self._auto_threshold(c, w):
                self._direction = 0
                self._adjust_timer.stop()
                return

        remaining = self._desired_ddc - self._current_ddc_float
        if abs(remaining) <= 1e-6:
            self._direction = 0
            self._adjust_timer.stop()
            return

        # 平方根曲線（方案 D）：step = sign(remaining) × sqrt(abs(remaining)) × k
        step = math.copysign(1.0, remaining) * math.sqrt(abs(remaining)) * self._k
        # 最低步階，避免停滯
        if 0 < abs(step) < 0.5:
            step = 0.5 if step > 0 else -0.5
        # 不 overshoot
        if abs(step) > abs(remaining):
            step = remaining

        if abs(step) <= 1e-9:
            self._direction = 0
            self._adjust_timer.stop()
            return

        # 樂觀前進內部記數器，讓剩餘值自然遞減、步階逐步收斂
        self._current_ddc_float += step
        self._current_ddc = int(round(self._current_ddc_float))

        self.adjust_requested.emit(step)

        if abs(self._desired_ddc - self._current_ddc_float) <= 1e-6:
            self._direction = 0
            self._adjust_timer.stop()


# =========================
# Network: mDNS Discovery + TCP Control
# =========================
NETWORK_SERVICE_TYPE = "_brightnessddc._tcp.local."
NETWORK_PORT = 9876

class NetworkMonitorServer:
    """在背景執行緒啟動 TCP server，透過 mDNS 廣播本機 DDC 螢幕資訊。
    
    客戶端連線後可取得所有螢幕列表，並可下達亮度/對比設定指令。
    通訊協定為 JSON line-based (一行一個 JSON 物件)。
    """
    def __init__(
        self,
        get_monitor_wrappers,
        set_monitor_callback,
        get_monitor_state_callback=None,
        get_app_state_callback=None,
        set_app_state_callback=None,
    ):
        self._get_wrappers = get_monitor_wrappers
        self._set_callback = set_monitor_callback
        self._get_monitor_state_callback = get_monitor_state_callback
        self._get_app_state_callback = get_app_state_callback
        self._set_app_state_callback = set_app_state_callback
        self._running = False
        self._server_thread = None
        self._sock = None
        self._zeroconf = None
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()

    def start(self):
        if self._running:
            return
        self._running = True
        self._server_thread = threading.Thread(target=self._run_server, daemon=True, name="NetServer")
        self._server_thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        if self._zeroconf:
            try:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
            except Exception:
                pass
        self._sock = None
        self._zeroconf = None

    def _run_server(self):
        import socket
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.settimeout(1.0)
            self._sock.bind(("0.0.0.0", NETWORK_PORT))
            self._sock.listen(5)
        except Exception as e:
            print(f"NetServer bind error: {e}")
            self._running = False
            return

        # 註冊 mDNS
        try:
            local_ip = self._get_local_ip()
            hostname = socket.gethostname()
            info = ServiceInfo(
                type_=NETWORK_SERVICE_TYPE,
                name=f"{hostname}-ddc.{NETWORK_SERVICE_TYPE}",
                addresses=[socket.inet_aton(local_ip)],
                port=NETWORK_PORT,
                properties={"hostname": hostname},
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(info)
        except Exception as e:
            print(f"mDNS register error: {e}")

        print(f"NetServer started on port {NETWORK_PORT}")
        while self._running:
            try:
                client, addr = self._sock.accept()
                client.settimeout(5.0)
                threading.Thread(target=self._handle_client, args=(client, addr), daemon=True).start()
            except TimeoutError:
                continue
            except Exception:
                if self._running:
                    continue
                break

    def _get_local_ip(self):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _handle_client(self, client, addr):
        import socket
        try:
            # 註冊為可接收廣播的 client
            with self._clients_lock:
                self._clients.append(client)
            with client:
                buf = b""
                while self._running:
                    try:
                        data = client.recv(4096)
                    except socket.timeout:
                        # timeout 用於讓循環可以中斷並維護連線清單
                        continue
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            log_network_payload("接收", json.loads(line))
                        except Exception:
                            pass
                        response = self._process_request(line)
                        should_broadcast = bool(response.pop("_broadcast_monitors", False))
                        try:
                            log_network_payload("發送", response)
                            client.sendall((json.dumps(response) + "\n").encode())
                            if should_broadcast:
                                self.broadcast_monitor_state()
                        except Exception:
                            # 無法回應則終止連線
                            raise
        except (socket.timeout, ConnectionResetError, OSError):
            pass
        except Exception as e:
            print(f"NetServer client error: {e}")
        finally:
            # 移除 client
            try:
                with self._clients_lock:
                    if client in self._clients:
                        self._clients.remove(client)
            except Exception:
                pass

    def broadcast_luminance(self, value: float, source: str = "—"):
        """向所有已連線 client 廣播亮度事件（JSON line）。"""
        payload = {"event": "luminance", "value": float(value), "source": source, "ts": time.time()}
        log_network_signal("發送", "畫面亮度", f"{float(value):.1f} source={source}")
        self._broadcast_payload(payload)

    def _monitor_snapshot(self):
        if self._get_monitor_state_callback is not None:
            try:
                return self._get_monitor_state_callback()
            except Exception as e:
                print(f"Monitor state snapshot error: {e}")

        monitors = []
        for w in self._get_wrappers():
            if isinstance(w, RemoteMonitorWrapper) or not w.available:
                continue
            b, c = w.read_current_levels()
            monitors.append({
                "name": w.name,
                "brightness": b,
                "contrast": c,
                "brightness_range": w.brightness_range,
                "contrast_range": w.contrast_range,
                "brightness_supported": w.brightness_supported,
                "contrast_supported": w.contrast_supported,
            })
        return monitors

    def broadcast_monitor_state(self):
        """向已訂閱 client 立即推送目前本機螢幕亮度狀態。"""
        payload = self._state_payload()
        payload["event"] = "monitors"
        payload["ts"] = time.time()
        log_network_payload("發送", payload)
        self._broadcast_payload(payload)

    def _app_state_snapshot(self):
        if self._get_app_state_callback is None:
            return {}
        try:
            state = self._get_app_state_callback()
            return state if isinstance(state, dict) else {}
        except Exception as e:
            print(f"App state snapshot error: {e}")
            return {}

    def _state_payload(self):
        payload = {"monitors": self._monitor_snapshot()}
        payload.update(self._app_state_snapshot())
        return payload

    def _broadcast_payload(self, payload):
        data = (json.dumps(payload) + "\n").encode()
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.sendall(data)
            except Exception:
                try:
                    with self._clients_lock:
                        if c in self._clients:
                            self._clients.remove(c)
                except Exception:
                    pass

    def _process_request(self, line):
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {"error": "invalid json"}
        cmd = req.get("cmd", "")
        if cmd == "list":
            return self._state_payload()
        elif cmd == "set":
            name = req.get("name", "")
            brightness = req.get("brightness")
            contrast = req.get("contrast")
            ok = bool(self._set_callback(name, brightness, contrast))
            response = {"ok": ok, "_broadcast_monitors": ok}
            response.update(self._state_payload())
            return response
        elif cmd == "set_state":
            if self._set_app_state_callback is None:
                return {"ok": False}
            ok = bool(self._set_app_state_callback(req.get("global_link"), req.get("auto_target"), req.get("auto_enabled")))
            response = {"ok": ok, "_broadcast_monitors": ok}
            response.update(self._state_payload())
            return response
        elif cmd == "subscribe":
            response = {"ok": True}
            response.update(self._state_payload())
            return response
        elif cmd == "ping":
            return {"pong": True}
        return {"error": "unknown cmd"}


class NetworkMonitorClient(QtCore.QObject):
    """mDNS 偵測網路上的 DDC server，提供遠端螢幕唯讀控制。"""
    remote_monitors_updated = QtCore.pyqtSignal(list)  # list of dict
    remote_state_updated = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._discovered_servers = {}  # name -> {"info": ServiceInfo, "monitors": [], "state": {}}
        self._subscriptions = {}
        self._subscriptions_lock = threading.Lock()
        self._browser = None
        self._zeroconf = None
        self._refresh_timer = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._zeroconf = Zeroconf()
        listener = _ServiceListener(self._on_service_changed)
        self._browser = ServiceBrowser(self._zeroconf, NETWORK_SERVICE_TYPE, listener)
        # 定期保底刷新；有訂閱連線時由 server 主動推送，避免重複回傳。
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_all)
        self._refresh_timer.start(10000)

    def stop(self):
        self._running = False
        for name in list(self._subscriptions.keys()):
            self._stop_subscription(name)
        if self._refresh_timer:
            self._refresh_timer.stop()
        if self._browser:
            try:
                self._browser.cancel()
            except Exception:
                pass
        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
        self._browser = None
        self._zeroconf = None

    def _on_service_changed(self, name, info=None, added=True):
        if not added:
            self._discovered_servers.pop(name, None)
            self._stop_subscription(name)
            self._emit_remote_monitors()
            return
        if info is None:
            return
        try:
            hostname = info.properties.get(b"hostname", b"").decode()
            if hostname and hostname == socket.gethostname():
                return
        except Exception:
            pass
        self._discovered_servers[name] = {"info": info, "monitors": [], "state": {}}
        self._query_server(name)
        self._start_subscription(name)

    def _query_server(self, name):
        entry = self._discovered_servers.get(name)
        if not entry:
            return
        info = entry["info"]
        if not info.parsed_addresses():
            return
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            addr = info.parsed_addresses()[0]
            s.connect((addr, info.port))
            s.sendall(json.dumps({"cmd": "list"}).encode() + b"\n")
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    line, _ = buf.split(b"\n", 1)
                    resp = json.loads(line)
                    self._handle_server_message(name, resp)
                    break
            s.close()
        except Exception as e:
            print(f"Query {name} error: {e}")

    def _refresh_all(self):
        for name in list(self._discovered_servers.keys()):
            with self._subscriptions_lock:
                state = self._subscriptions.get(name)
                if state and state.get("running") and state.get("socket") is not None:
                    continue
            self._query_server(name)

    def _emit_remote_monitors(self):
        all_monitors = []
        for srv_name, entry in self._discovered_servers.items():
            hostname = entry["info"].properties.get(b"hostname", srv_name).decode()
            addr = ""
            try:
                addrs = entry["info"].parsed_addresses()
                if addrs:
                    addr = addrs[0]
            except Exception:
                pass
            for mon in entry.get("monitors", []):
                mon["_remote_server"] = hostname
                mon["_remote_name"] = srv_name
                mon["_remote_address"] = addr
                all_monitors.append(mon)
        self.remote_monitors_updated.emit(all_monitors)

    def _handle_server_message(self, name, message):
        if not isinstance(message, dict):
            return
        if "monitors" not in message:
            return
        entry = self._discovered_servers.get(name)
        if not entry:
            return
        monitors = message.get("monitors", [])
        normalized_monitors = [dict(m) for m in monitors if isinstance(m, dict)] if isinstance(monitors, list) else []
        state = {}
        if "auto_target" in message:
            state["auto_target"] = message.get("auto_target")
        if "auto_enabled" in message:
            state["auto_enabled"] = message.get("auto_enabled")
        signature = json.dumps({"monitors": normalized_monitors, "state": state}, sort_keys=True, ensure_ascii=False)
        if entry.get("_last_message_signature") == signature:
            return
        entry["_last_message_signature"] = signature
        log_network_payload("接收", message)
        entry["monitors"] = normalized_monitors
        entry["state"] = state
        info = entry["info"]
        hostname = info.properties.get(b"hostname", b"unknown").decode()
        monitor_count = len(entry["monitors"])
        if entry.get("_last_monitor_count") != monitor_count:
            print(f"Remote server {hostname}: {monitor_count} monitors")
            entry["_last_monitor_count"] = monitor_count
        self._emit_remote_monitors()
        if state:
            state["_remote_server"] = hostname
            state["_remote_name"] = name
            self.remote_state_updated.emit(state)

    def _start_subscription(self, name):
        with self._subscriptions_lock:
            existing = self._subscriptions.get(name)
            if existing and existing.get("running"):
                return
            state = {"running": True, "socket": None}
            self._subscriptions[name] = state
        thread = threading.Thread(target=self._subscription_loop, args=(name, state), daemon=True, name="NetClientSub")
        state["thread"] = thread
        thread.start()

    def _stop_subscription(self, name):
        with self._subscriptions_lock:
            state = self._subscriptions.pop(name, None)
        if not state:
            return
        state["running"] = False
        sock = state.get("socket")
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _subscription_loop(self, name, state):
        while self._running and state.get("running"):
            entry = self._discovered_servers.get(name)
            if not entry:
                break
            info = entry["info"]
            if not info.parsed_addresses():
                time.sleep(1.0)
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    state["socket"] = s
                    s.settimeout(1.0)
                    s.connect((info.parsed_addresses()[0], info.port))
                    s.sendall(json.dumps({"cmd": "subscribe"}).encode() + b"\n")
                    buf = b""
                    while self._running and state.get("running"):
                        try:
                            chunk = s.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            self._handle_server_message(name, json.loads(line))
            except Exception as e:
                if self._running and state.get("running"):
                    print(f"Subscribe {name} error: {e}")
                    time.sleep(2.0)
            finally:
                state["socket"] = None

    def remote_set(self, server_name, monitor_name, brightness=None, contrast=None):
        entry = self._discovered_servers.get(server_name)
        if not entry:
            return False
        info = entry["info"]
        if not info.parsed_addresses():
            return False
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect((info.parsed_addresses()[0], info.port))
                req = {"cmd": "set", "name": monitor_name}
                if brightness is not None:
                    req["brightness"] = int(brightness)
                if contrast is not None:
                    req["contrast"] = int(contrast)
                log_network_payload("發送", req)
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        line, _ = buf.split(b"\n", 1)
                        resp = json.loads(line)
                        ok = bool(resp.get("ok"))
                        return ok
        except Exception as e:
            print(f"Remote set error: {e}")
        return False

    def remote_set_state(self, server_name, auto_target=None, auto_enabled=None):
        entry = self._discovered_servers.get(server_name)
        if not entry:
            return False
        info = entry["info"]
        if not info.parsed_addresses():
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect((info.parsed_addresses()[0], info.port))
                req = {"cmd": "set_state"}
                if auto_target is not None:
                    req["auto_target"] = int(auto_target)
                if auto_enabled is not None:
                    req["auto_enabled"] = bool(auto_enabled)
                log_network_payload("發送", req)
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\n" in buf:
                        line, _ = buf.split(b"\n", 1)
                        resp = json.loads(line)
                        ok = bool(resp.get("ok"))
                        return ok
        except Exception as e:
            print(f"Remote set state error: {e}")
        return False


class _ServiceListener:
    """Zeroconf service listener callback wrapper."""
    def __init__(self, callback):
        self._callback = callback

    def add_service(self, zeroconf, type_, name):
        info = zeroconf.get_service_info(type_, name)
        if info:
            self._callback(name, info, True)

    def remove_service(self, zeroconf, type_, name):
        self._callback(name, None, False)

    def update_service(self, zeroconf, type_, name):
        self.remove_service(zeroconf, type_, name)
        self.add_service(zeroconf, type_, name)


class _RefreshDetectWorker(QtCore.QObject):
    """在背景執行完整螢幕偵測（DDC 操作），完成後以 signal 回傳結果。"""
    detection_complete = QtCore.pyqtSignal(object)  # dict with detection results

    def __init__(self, old_wrappers: list):
        super().__init__()
        self._old_wrappers = old_wrappers

    def run(self):
        """在背景執行緒執行。"""
        result = {
            "fresh_wrappers": [],
            "detected_monitors": [],
            "count": 0,
            "error": None,
        }

        try:
            # ── 關閉所有舊的 DDC 連線 ──
            for w in self._old_wrappers:
                if isinstance(w, RemoteMonitorWrapper):
                    continue
                try:
                    with w.lock:
                        if w.monitor is not None:
                            try:
                                w.monitor.__exit__(None, None, None)
                            except Exception:
                                pass
                            try:
                                del w.monitor
                            except Exception:
                                pass
                            w.monitor = None
                except Exception:
                    pass

            import gc
            gc.collect()

            # ── 多次重試 get_monitors() ──
            detected_monitors = []
            for attempt in range(5):
                try:
                    detected_monitors = _run_ddc_with_timeout(
                        lambda: list(get_monitors()),
                        timeout_sec=3.0,
                        default=[],
                    )
                    if detected_monitors:
                        break
                except Exception:
                    pass
                if attempt < 4:
                    import time as _time
                    _time.sleep(0.5 * (attempt + 1))

            # ── 保留舊範圍 ──
            preserved_ranges = {}
            for w in self._old_wrappers:
                if isinstance(w, RemoteMonitorWrapper):
                    continue
                if _is_valid_monitor_name(w.name):
                    preserved_ranges[w.name] = (
                        list(w.brightness_range),
                        list(w.contrast_range),
                    )

            # ── 建立新 wrappers ──
            fresh_wrappers = []
            for i, m in enumerate(detected_monitors):
                try:
                    w = _run_ddc_with_timeout(
                        lambda m=m, i=i: MonitorWrapper(m, i),
                        timeout_sec=3.0,
                        default=None,
                    )
                    if w is not None:
                        name = w.name
                        if name in preserved_ranges:
                            b_range, c_range = preserved_ranges[name]
                            w.brightness_range = list(b_range)
                            w.contrast_range = list(c_range)
                        fresh_wrappers.append(w)
                except Exception:
                    pass

            # 若無偵測到可用螢幕，保留舊不可用 wrapper 作為佔位
            if not fresh_wrappers:
                for w in self._old_wrappers:
                    if not isinstance(w, RemoteMonitorWrapper):
                        w.available = False
                        w.monitor = None
                        fresh_wrappers.append(w)

            result["fresh_wrappers"] = fresh_wrappers
            result["count"] = len(detected_monitors)

        except Exception as e:
            result["error"] = str(e)

        self.detection_complete.emit(result)


class RemoteMonitorWrapper:
    """遠端螢幕的 MonitorWrapper 等價物件（唯讀/可遠端設定）。"""
    def __init__(self, data, server_name):
        self.name = data.get("name", f"Remote {server_name}")
        self._server_name = server_name
        self._server_hostname = data.get("_remote_server", server_name)
        self.brightness_range = list(data.get("brightness_range", [0, 100]))
        self.contrast_range = list(data.get("contrast_range", [0, 100]))
        self.brightness_supported = data.get("brightness_supported", True)
        self.contrast_supported = data.get("contrast_supported", True)
        self.supported = True
        self.available = True
        self._brightness = data.get("brightness")
        self._contrast = data.get("contrast")

    def read_current_levels(self):
        return self._brightness, self._contrast

    def update_from_data(self, data):
        self._brightness = data.get("brightness", self._brightness)
        self._contrast = data.get("contrast", self._contrast)
        self.brightness_range = list(data.get("brightness_range", self.brightness_range))
        self.contrast_range = list(data.get("contrast_range", self.contrast_range))


# =========================
# Main Window
# =========================
class MainWindow(QtWidgets.QWidget):
    remote_set_applied = QtCore.pyqtSignal(str, object, object)
    remote_state_applied = QtCore.pyqtSignal(object, object, object)
    HOTKEY_OPTIONS = MODIFIER_ORDER
    HOTKEY_OPTIONAL_OPTIONS = SHORTCUT_MODIFIER_OPTIONS
    STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    STARTUP_VALUE_NAME = "BrightnessDDCController"

    def __init__(self):
        super().__init__()

        self.setWindowTitle("DDC/CI Controller")
        self.threadpool = QtCore.QThreadPool()
        self._is_quitting = False
        self._updating_global_link = False
        self._applying_remote_network_state = False
        self._pending_network_auto_target = None
        self._pending_network_auto_enabled = None
        self.global_link_value = 0
        self.step_value = 5.0
        self.shortcut_key1 = "Alt"
        self.shortcut_key2 = "Win"
        self.shortcut_key3 = "None"
        self.auto_start_enabled = False
        self.network_debug_enabled = False
        self.level_shortcuts = [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
        self.global_hook = None
        self._loading_settings = False

        # 網路功能
        self._network_server_enabled = False
        self._network_client_enabled = False
        self._network_mode = "disabled"
        self._remote_wrappers = []
        self._remote_widgets = []
        self._remote_monitor_data = []
        self.remote_servers_map = {}
        self._pending_remote_sets = {}

        # 畫面自動調整
        self.auto_adjust_enabled = False
        self.auto_adjust_target = 50
        self.auto_adjust_k = 0.8
        self.auto_adjust_weight = AUTO_BRIGHTNESS_WEIGHT_DEFAULT
        self.auto_adjust_capture_interval = 1.0
        self.auto_adjust_tick_interval = 200
        self.auto_adjust_resource_saving_enabled = True
        self.auto_adjust_resource_saving_idle_seconds = 5.0
        self.screen_analyzers = []
        self.screen_analyzer = None
        self._monitor_auto_states = []
        self.current_effective_brightness = None

        # 網路功能
        self._network_server_enabled = False
        self._network_client_enabled = False
        self._network_mode = "disabled"
        self._net_server = NetworkMonitorServer(
            get_monitor_wrappers=lambda: self.monitor_wrappers,
            set_monitor_callback=self._remote_set_monitor,
            get_monitor_state_callback=self._network_monitor_snapshot,
            get_app_state_callback=self._network_app_state_snapshot,
            set_app_state_callback=self._remote_set_app_state,
        )
        self._net_client = NetworkMonitorClient(self)
        self._net_client.remote_monitors_updated.connect(self._on_remote_monitors_updated)
        self._net_client.remote_state_updated.connect(self._on_remote_state_updated)
        self.remote_set_applied.connect(self._on_remote_set_applied)
        self.remote_state_applied.connect(self._on_remote_state_applied)
        self._remote_wrappers = []  # RemoteMonitorWrapper 列表
        self._remote_widgets = []   # 遠端螢幕的 MonitorWidget
        self._remote_monitor_data = []  # 原始資料（用於 remote_set）

        # 初始螢幕偵測 — 給足夠時間（5s）讓 DDC 列舉完成
        self.monitor_wrappers = []
        try:
            detected = _run_ddc_with_timeout(lambda: list(get_monitors()), timeout_sec=5.0, default=[])
        except Exception:
            detected = []
        for i, m in enumerate(detected):
            try:
                w = _run_ddc_with_timeout(lambda m=m, i=i: MonitorWrapper(m, i), timeout_sec=3.0, default=None)
                if w is not None:
                    self.monitor_wrappers.append(w)
            except Exception:
                pass

        self._prev_raw_monitor_count = len(detected)

        # 完全沒偵測到時，從設定恢復已知螢幕佔位
        if not self.monitor_wrappers:
            self._restore_known_monitors_from_settings()

        # 在建 UI 之前先套用已儲存的範圍（與 refresh_monitors 流程一致）
        self._reload_monitor_ranges_from_settings()

        self.monitor_widgets = []
        self.monitor_range_widgets = []
        self.shortcut_rows = []
        self._update_analyzer_levels()

        # 防抖存檔 Timer (避免頻繁寫入硬碟)
        self.save_timer = QtCore.QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_settings)

        # 熱插拔偵測（優先使用 WMI 事件監聽，降級到 5 秒輪詢）
        self._hotplug_watcher = _MonitorHotplugWatcher(self)
        self._hotplug_watcher.monitors_changed.connect(self._on_hotplug_event)
        self._hotplug_watcher.start(poll_interval_ms=5000)
        # 熱插拔防彈跳：連續事件只在最後一次後等待 1.5 秒才觸發 refresh
        self._hotplug_debounce_timer = QtCore.QTimer(self)
        self._hotplug_debounce_timer.setSingleShot(True)
        self._hotplug_debounce_timer.timeout.connect(self._do_hotplug_refresh)

        root_layout = QtWidgets.QVBoxLayout()
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(5)
        root_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)

        self.stack = QtWidgets.QStackedWidget()
        self.main_page = self.build_main_page()
        self.settings_page = self.build_settings_page()
        self.settings_page.setMinimumWidth(600)
        self.stack.addWidget(self.main_page)
        self.stack.addWidget(self.settings_page)
        root_layout.addWidget(self.stack)
        self.setLayout(root_layout)

        self.init_tray()
        self.init_global_hook()

        self.load_settings()
        self.show_main_page()
        # 立即啟動畫面分析器（使用 inline 偵測到的螢幕）
        self._init_screen_analyzers()
        # 若沒抓到可用螢幕，3 秒後完整重試（背景重試仍會載入設定）
        if not self._has_available_local_monitor():
            QtCore.QTimer.singleShot(3000, self.refresh_monitors)

    def _has_available_local_monitor(self):
        return any(
            getattr(wrapper, "available", False) and not isinstance(wrapper, RemoteMonitorWrapper)
            for wrapper in self.monitor_wrappers
        )

    def _restore_known_monitors_from_settings(self):
        """從設定檔恢復已知螢幕名稱，建立不可用的 MonitorWrapper 佔位"""
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            known_names = saved.get("known_monitor_names", [])
            monitors_data = saved.get("monitors", {})
            cleaned_names = []
            for i, name in enumerate(known_names):
                if not _is_valid_monitor_name(name):
                    print(f"Skipping invalid monitor name from settings: {name!r}")
                    continue
                b_range = [0, 100]
                c_range = [0, 100]
                # 支援新格式（name-keyed dict）與舊格式（positional array）
                if isinstance(monitors_data, dict):
                    saved_entry = monitors_data.get(name)
                    if saved_entry:
                        b_range = list(saved_entry.get("b_range", [0, 100]))
                        c_range = list(saved_entry.get("c_range", [0, 100]))
                elif isinstance(monitors_data, list) and i < len(monitors_data):
                    b_range = list(monitors_data[i].get("b_range", [0, 100]))
                    c_range = list(monitors_data[i].get("c_range", [0, 100]))
                w = MonitorWrapper(monitor=None, index=i, name=name,
                                   b_range=b_range, c_range=c_range)
                w.available = False
                w.supported = False
                self.monitor_wrappers.append(w)
                cleaned_names.append(name)
        except Exception:
            pass

    def _on_hotplug_event(self):
        """收到熱插拔事件 → 重設計時器等待 1.5 秒，避免連環觸發。"""
        if self._loading_settings or self._is_quitting or getattr(self, "_refresh_in_progress", False):
            return
        self._hotplug_debounce_timer.start(1500)

    def _do_hotplug_refresh(self):
        """防彈跳計時器到期後，檢查螢幕數量是否改變並執行 refresh。"""
        if self._is_quitting:
            return
        try:
            current_count = _run_ddc_with_timeout(
                lambda: len(list(get_monitors())),
                timeout_sec=2.0,
                default=-1,
            )
        except Exception:
            current_count = -1
        if current_count >= 0 and current_count != self._prev_raw_monitor_count:
            print(f"Monitor count changed: {self._prev_raw_monitor_count} → {current_count}")
            self.refresh_monitors()
        elif current_count < 0:
            self.refresh_monitors()

    def _configure_screen_analyzer(self, analyzer):
        analyzer.enabled = self.auto_adjust_enabled
        analyzer.target = self.auto_adjust_target
        analyzer.set_k(self.auto_adjust_k)
        analyzer.weight = self.auto_adjust_weight
        analyzer.set_capture_interval_seconds(self.auto_adjust_capture_interval)
        analyzer.set_tick_interval_ms(self.auto_adjust_tick_interval)
        analyzer.set_resource_saving(
            self.auto_adjust_resource_saving_enabled,
            self.auto_adjust_resource_saving_idle_seconds,
        )

    def _init_screen_analyzers(self):
        for analyzer in getattr(self, "screen_analyzers", []):
            if analyzer is not None:
                analyzer.stop()

        dxgi_targets = get_dxgi_display_targets()
        if dxgi_targets:
            target_text = ", ".join(
                f"{i}:D{t['device_idx']}O{t['output_idx']}{'*' if t.get('primary') else ''}"
                for i, t in enumerate(dxgi_targets)
            )
            print(f"DXGI targets: {target_text}")

        self.screen_analyzers = []
        self._monitor_auto_states = []
        for idx, wrapper in enumerate(self.monitor_wrappers):
            self._monitor_auto_states.append({"avg": None, "source": "—", "current": None})
            if isinstance(wrapper, RemoteMonitorWrapper):
                self.screen_analyzers.append(None)
                continue

            if not dxgi_targets or idx >= len(dxgi_targets):
                print(f"DXGI target missing for monitor {idx}: {wrapper.name}")
                self.screen_analyzers.append(None)
                continue

            dxgi_target = dxgi_targets[idx]
            analyzer = ScreenAnalyzer(self, output_idx=idx, dxgi_target=dxgi_target)
            self._configure_screen_analyzer(analyzer)
            analyzer.adjust_requested.connect(lambda delta, i=idx: self.on_screen_adjust_requested(i, delta))
            analyzer.luminance_updated.connect(lambda lum, i=idx: self.on_luminance_updated(i, lum))
            analyzer.luminance_source_updated.connect(lambda source, i=idx: self._on_luminance_source_updated(i, source))
            analyzer.start()
            self.screen_analyzers.append(analyzer)

        self.screen_analyzer = next((a for a in self.screen_analyzers if a is not None), None)
        self._sync_screen_analyzer_enabled()

    def _for_each_screen_analyzer(self, callback):
        for analyzer in getattr(self, "screen_analyzers", []):
            if analyzer is not None:
                callback(analyzer)

    def _sync_screen_analyzer_enabled(self):
        for idx, analyzer in enumerate(getattr(self, "screen_analyzers", [])):
            if analyzer is not None:
                wrapper = self.monitor_wrappers[idx] if idx < len(self.monitor_wrappers) else None
                analyzer.enabled = self.auto_adjust_enabled and bool(getattr(wrapper, "available", False))

    def build_main_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        top_bar = QtWidgets.QHBoxLayout()
        self.main_auto_adjust_checkbox = QtWidgets.QCheckBox("自動調整")
        self.main_auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
        self.main_auto_adjust_checkbox.toggled.connect(self.on_auto_adjust_toggled)
        top_bar.addWidget(self.main_auto_adjust_checkbox)
        top_bar.addStretch()
        refresh_button = QtWidgets.QPushButton("重新偵測")
        refresh_button.clicked.connect(self.refresh_monitors)
        top_bar.addWidget(refresh_button)
        settings_button = QtWidgets.QPushButton("設定")
        settings_button.clicked.connect(self.show_settings_page)
        top_bar.addWidget(settings_button)
        layout.addLayout(top_bar)

        self.auto_target_group = QtWidgets.QGroupBox("自動調整目標亮度")
        auto_target_layout = QtWidgets.QGridLayout()
        auto_target_layout.setContentsMargins(6, 6, 6, 6)
        auto_target_layout.setSpacing(6)
        self.auto_target_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.auto_target_slider.setRange(0, 100)
        self.auto_target_slider.setValue(self.auto_adjust_target)
        self.auto_target_value_label = QtWidgets.QLabel(str(self.auto_adjust_target))
        self.auto_target_value_label.setMinimumWidth(30)
        self.auto_target_slider.valueChanged.connect(self.on_main_target_slider_changed)

        self.main_global_link_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.main_global_link_slider.setRange(0, 100)
        self.main_global_link_slider.setValue(int(self.global_link_value))
        self.main_global_link_value_label = QtWidgets.QLabel(str(int(self.global_link_value)))
        self.main_global_link_value_label.setMinimumWidth(30)
        self.main_global_link_slider.valueChanged.connect(self.on_main_global_link_slider_changed)

        auto_target_layout.addWidget(QtWidgets.QLabel("Target"), 0, 0)
        auto_target_layout.addWidget(self.auto_target_slider, 0, 1)
        auto_target_layout.addWidget(self.auto_target_value_label, 0, 2)
        auto_target_layout.addWidget(QtWidgets.QLabel("Global Link"), 1, 0)
        auto_target_layout.addWidget(self.main_global_link_slider, 1, 1)
        auto_target_layout.addWidget(self.main_global_link_value_label, 1, 2)
        self.auto_target_group.setLayout(auto_target_layout)
        layout.addWidget(self.auto_target_group)

        if not self.monitor_wrappers:
            self.monitor_container = QtWidgets.QWidget()
            self.monitor_container_layout = QtWidgets.QVBoxLayout()
            self.monitor_container_layout.setContentsMargins(0, 0, 0, 0)
            self.monitor_container_layout.setSpacing(5)
            self.monitor_container.setLayout(self.monitor_container_layout)
            label = QtWidgets.QLabel("未偵測到可控制的螢幕")
            self.monitor_container_layout.addWidget(label)
            layout.addWidget(self.monitor_container)
        else:
            # 監視器容器：固定位置，內部 widget 重建時不影響外層 layout
            self.monitor_container = QtWidgets.QWidget()
            self.monitor_container_layout = QtWidgets.QVBoxLayout()
            self.monitor_container_layout.setContentsMargins(0, 0, 0, 0)
            self.monitor_container_layout.setSpacing(5)
            self.monitor_container.setLayout(self.monitor_container_layout)
            for wrapper in self.monitor_wrappers:
                monitor_widget = MonitorWidget(wrapper, self.threadpool)
                monitor_widget.value_changed.connect(self.on_monitor_link_changed)
                monitor_widget.set_available(wrapper.available)
                self.monitor_widgets.append(monitor_widget)
                self.monitor_container_layout.addWidget(monitor_widget)
            layout.addWidget(self.monitor_container)

        layout.addStretch()
        page.setLayout(layout)
        return page

    def build_settings_page(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addStretch()
        back_button = QtWidgets.QPushButton("返回主介面")
        back_button.clicked.connect(self.show_main_page)
        top_bar.addWidget(back_button)
        layout.addLayout(top_bar)

        # Tab widget: General / Monitors / Network
        tabs = QtWidgets.QTabWidget()

        # --- General Tab ---
        gen_scroll = QtWidgets.QScrollArea()
        gen_scroll.setWidgetResizable(True)
        gen_container = QtWidgets.QWidget()
        gen_layout = QtWidgets.QVBoxLayout()
        gen_layout.setContentsMargins(0, 0, 0, 0)
        gen_layout.setSpacing(8)

        # 全局設定 (Step, autostart)
        global_group = QtWidgets.QGroupBox("全局設定")
        global_grid = QtWidgets.QGridLayout()
        global_grid.setContentsMargins(6, 6, 6, 6)
        global_grid.setSpacing(6)

        self.step_combo = QtWidgets.QComboBox()
        STEP_OPTIONS = ["1", "2", "2.5", "4", "5","10"]
        self.step_combo.addItems(STEP_OPTIONS)
        default_idx = 0
        for i, s in enumerate(STEP_OPTIONS):
            if abs(float(s) - self.step_value) < 0.01:
                default_idx = i
                break
        self.step_combo.setCurrentIndex(default_idx)
        self.step_combo.currentTextChanged.connect(self.on_step_changed)

        self.autostart_checkbox = QtWidgets.QCheckBox("開機自啟")
        self.autostart_checkbox.toggled.connect(self.on_autostart_toggled)
        if sys.platform == "win32":
            self.auto_start_enabled = self.is_startup_enabled()
        self.autostart_checkbox.setChecked(self.auto_start_enabled)

        self.network_debug_checkbox = QtWidgets.QCheckBox("Debug 網路狀態")
        self.network_debug_checkbox.setChecked(self.network_debug_enabled)
        self.network_debug_checkbox.toggled.connect(self.on_network_debug_toggled)

        global_grid.addWidget(QtWidgets.QLabel("Step"), 0, 0)
        global_grid.addWidget(self.step_combo, 0, 1)
        global_grid.addWidget(self.autostart_checkbox, 1, 0, 1, 2)
        global_grid.addWidget(self.network_debug_checkbox, 2, 0, 1, 2)
        global_group.setLayout(global_grid)
        gen_layout.addWidget(global_group)

        # 滾輪快捷鍵 + 鍵盤快捷鍵放 General
        wheel_group = QtWidgets.QGroupBox("滾輪快捷鍵")
        wheel_grid = QtWidgets.QGridLayout()
        wheel_grid.setContentsMargins(6, 6, 6, 6)
        wheel_grid.setSpacing(6)

        self.shortcut_key1_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.shortcut_key2_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.shortcut_key3_button = KeyCaptureButton(allow_none=True, allow_modifiers=True)
        self.shortcut_key1_button.set_key_name(self.shortcut_key1)
        self.shortcut_key2_button.set_key_name(self.shortcut_key2)
        self.shortcut_key3_button.set_key_name(self.shortcut_key3)
        self.shortcut_key1_button.key_changed.connect(self.on_shortcut_changed)
        self.shortcut_key2_button.key_changed.connect(self.on_shortcut_changed)
        self.shortcut_key3_button.key_changed.connect(self.on_shortcut_changed)

        wheel_grid.addWidget(QtWidgets.QLabel("觸發鍵"), 0, 0)
        wheel_grid.addWidget(self.shortcut_key1_button, 0, 1)
        wheel_grid.addWidget(QtWidgets.QLabel("+"), 0, 2)
        wheel_grid.addWidget(self.shortcut_key2_button, 0, 3)
        wheel_grid.addWidget(QtWidgets.QLabel("+"), 0, 4)
        wheel_grid.addWidget(self.shortcut_key3_button, 0, 5)
        wheel_group.setLayout(wheel_grid)
        gen_layout.addWidget(wheel_group)

        shortcut_group = QtWidgets.QGroupBox("鍵盤快捷鍵")
        shortcut_layout = QtWidgets.QVBoxLayout()
        shortcut_layout.setContentsMargins(6, 6, 6, 6)
        shortcut_layout.setSpacing(6)

        shortcut_hint = QtWidgets.QLabel("可任意新增或刪除快捷鍵。點擊按鍵欄位後直接按下要使用的按鍵，最多三個鍵。")
        shortcut_hint.setWordWrap(True)
        shortcut_layout.addWidget(shortcut_hint)

        self.shortcut_rows_container = QtWidgets.QWidget()
        self.shortcut_rows_layout = QtWidgets.QVBoxLayout()
        self.shortcut_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.shortcut_rows_layout.setSpacing(4)
        self.shortcut_rows_container.setLayout(self.shortcut_rows_layout)
        shortcut_layout.addWidget(self.shortcut_rows_container)

        add_shortcut_button = QtWidgets.QPushButton("新增快捷鍵")
        add_shortcut_button.clicked.connect(self.add_shortcut_row)
        shortcut_layout.addWidget(add_shortcut_button)
        shortcut_group.setLayout(shortcut_layout)
        gen_layout.addWidget(shortcut_group)

        gen_layout.addStretch()
        gen_container.setLayout(gen_layout)
        gen_scroll.setWidget(gen_container)
        tabs.addTab(gen_scroll, "General")

        # --- Monitors Tab ---
        mon_scroll = QtWidgets.QScrollArea()
        mon_scroll.setWidgetResizable(True)
        mon_container = QtWidgets.QWidget()
        mon_layout = QtWidgets.QVBoxLayout()
        mon_layout.setContentsMargins(0, 0, 0, 0)
        mon_layout.setSpacing(8)

        # 畫面自動調整
        auto_group = QtWidgets.QGroupBox("畫面自動調整")
        auto_grid = QtWidgets.QGridLayout()
        auto_grid.setContentsMargins(6, 6, 6, 6)
        auto_grid.setSpacing(6)

        self.auto_adjust_checkbox = QtWidgets.QCheckBox("啟用根據畫面內容自動調整亮度")
        self.auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
        self.auto_adjust_checkbox.toggled.connect(self.on_auto_adjust_toggled)

        self.auto_adjust_k_spin = QtWidgets.QDoubleSpinBox()
        self.auto_adjust_k_spin.setRange(0.1, 5.0)
        self.auto_adjust_k_spin.setSingleStep(0.1)
        self.auto_adjust_k_spin.setDecimals(2)
        self.auto_adjust_k_spin.setValue(self.auto_adjust_k)
        self.auto_adjust_k_spin.setToolTip("平方根曲線係數，越大越快。預設 0.8")
        self.auto_adjust_k_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

        self.auto_adjust_weight_spin = QtWidgets.QDoubleSpinBox()
        self.auto_adjust_weight_spin.setRange(0.1, 10.0)
        self.auto_adjust_weight_spin.setSingleStep(0.1)
        self.auto_adjust_weight_spin.setDecimals(2)
        self.auto_adjust_weight_spin.setValue(self.auto_adjust_weight)
        self.auto_adjust_weight_spin.setToolTip("背光權重，預設 1.0")
        self.auto_adjust_weight_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

        self.auto_adjust_capture_interval_spin = QtWidgets.QDoubleSpinBox()
        self.auto_adjust_capture_interval_spin.setRange(0.1, 5.0)
        self.auto_adjust_capture_interval_spin.setSingleStep(0.1)
        self.auto_adjust_capture_interval_spin.setDecimals(1)
        self.auto_adjust_capture_interval_spin.setValue(self.auto_adjust_capture_interval)
        self.auto_adjust_capture_interval_spin.setSuffix(" s")
        self.auto_adjust_capture_interval_spin.setToolTip("畫面截圖分析週期")
        self.auto_adjust_capture_interval_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

        self.auto_adjust_resource_saving_checkbox = QtWidgets.QCheckBox("啟用資源節省模式")
        self.auto_adjust_resource_saving_checkbox.setChecked(self.auto_adjust_resource_saving_enabled)
        self.auto_adjust_resource_saving_checkbox.toggled.connect(self.on_auto_adjust_settings_changed)

        self.auto_adjust_resource_saving_idle_spin = QtWidgets.QDoubleSpinBox()
        self.auto_adjust_resource_saving_idle_spin.setRange(0.1, 60.0)
        self.auto_adjust_resource_saving_idle_spin.setSingleStep(0.1)
        self.auto_adjust_resource_saving_idle_spin.setDecimals(1)
        self.auto_adjust_resource_saving_idle_spin.setValue(self.auto_adjust_resource_saving_idle_seconds)
        self.auto_adjust_resource_saving_idle_spin.setSuffix(" s")
        self.auto_adjust_resource_saving_idle_spin.setToolTip("畫面亮度差異為 0 持續多久後，開始倍增截圖間隔")
        self.auto_adjust_resource_saving_idle_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

        self.auto_adjust_tick_interval_spin = QtWidgets.QSpinBox()
        self.auto_adjust_tick_interval_spin.setRange(10, 2000)
        self.auto_adjust_tick_interval_spin.setSingleStep(10)
        self.auto_adjust_tick_interval_spin.setValue(self.auto_adjust_tick_interval)
        self.auto_adjust_tick_interval_spin.setSuffix(" ms")
        self.auto_adjust_tick_interval_spin.setToolTip("每次微調的間隔時間（毫秒）")
        self.auto_adjust_tick_interval_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

        self.auto_formula_label = QtWidgets.QLabel()
        self.auto_formula_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.auto_formula_label.setWordWrap(True)
        self.auto_formula_label.setStyleSheet("color: gray; font-size: 10px;")
        self._update_auto_formula_label()

        auto_grid.addWidget(self.auto_adjust_checkbox, 0, 0, 1, 4)
        auto_grid.addWidget(QtWidgets.QLabel("截圖間隔"), 1, 0)
        auto_grid.addWidget(self.auto_adjust_capture_interval_spin, 1, 1)
        auto_grid.addWidget(QtWidgets.QLabel("曲線係數 k"), 1, 2)
        auto_grid.addWidget(self.auto_adjust_k_spin, 1, 3)
        auto_grid.addWidget(QtWidgets.QLabel("背光權重"), 2, 0)
        auto_grid.addWidget(QtWidgets.QLabel("背光權重"), 2, 2)
        auto_grid.addWidget(self.auto_adjust_weight_spin, 2, 3)
        auto_grid.addWidget(self.auto_adjust_resource_saving_checkbox, 3, 0, 1, 2)
        auto_grid.addWidget(QtWidgets.QLabel("靜止門檻"), 3, 2)
        auto_grid.addWidget(self.auto_adjust_resource_saving_idle_spin, 3, 3)
        auto_grid.addWidget(QtWidgets.QLabel("調整間隔"), 4, 0)
        auto_grid.addWidget(self.auto_adjust_tick_interval_spin, 4, 1)
        auto_grid.addWidget(self.auto_formula_label, 5, 0, 1, 4)
        auto_group.setLayout(auto_grid)
        mon_layout.addWidget(auto_group)

        if not self.monitor_wrappers:
            mon_layout.addWidget(QtWidgets.QLabel("未偵測到可控制的螢幕"))

        for wrapper, monitor_widget in zip(self.monitor_wrappers, self.monitor_widgets):
            range_widget = MonitorRangeWidget(wrapper)
            range_widget.ranges_changed.connect(monitor_widget.set_ranges)
            range_widget.ranges_changed.connect(lambda _b, _c: self.trigger_save())
            range_widget.ranges_changed.connect(lambda _b, _c: self._update_analyzer_levels())
            self.monitor_range_widgets.append(range_widget)
            mon_layout.addWidget(range_widget)

        mon_layout.addStretch()
        mon_container.setLayout(mon_layout)
        mon_scroll.setWidget(mon_container)
        tabs.addTab(mon_scroll, "Monitors")

        # --- Network Tab ---
        net_scroll = QtWidgets.QScrollArea()
        net_scroll.setWidgetResizable(True)
        net_container = QtWidgets.QWidget()
        net_layout = QtWidgets.QVBoxLayout()
        net_layout.setContentsMargins(0, 0, 0, 0)
        net_layout.setSpacing(8)

        net_group = QtWidgets.QGroupBox("網路功能 (DDC over LAN)")
        net_grid = QtWidgets.QGridLayout()
        net_grid.setContentsMargins(6, 6, 6, 6)
        net_grid.setSpacing(6)

        self.net_mode_combo = QtWidgets.QComboBox()
        self.net_mode_combo.addItems(["停用", "啟用伺服器", "啟用用戶端"])
        self.net_mode_combo.setCurrentIndex({"disabled": 0, "server": 1, "client": 2}.get(self._network_mode, 0))
        self.net_mode_combo.currentIndexChanged.connect(self._on_net_mode_changed)

        self.net_servers_label = QtWidgets.QLabel("已發現伺服器: 0")
        self.net_servers_label.setWordWrap(True)

        net_grid.addWidget(QtWidgets.QLabel("模式"), 0, 0)
        net_grid.addWidget(self.net_mode_combo, 0, 1)
        net_grid.addWidget(self.net_servers_label, 1, 0, 1, 2)
        net_group.setLayout(net_grid)
        net_layout.addWidget(net_group)
        net_layout.addStretch()
        net_container.setLayout(net_layout)
        net_scroll.setWidget(net_container)
        tabs.addTab(net_scroll, "Network")

        layout.addWidget(tabs)
        page.setLayout(layout)
        return page

    def show_main_page(self):
        self.stack.setCurrentWidget(self.main_page)
        self.show()
        self.raise_()
        self.activateWindow()

    def refresh_monitors(self):
        """完整重啟偵測流程：釋放舊資源、多次重試、完全重建 UI。
        等同於軟重啟，解決大多數熱插拔問題而不需關閉程式。"""
        if getattr(self, "_refresh_in_progress", False):
            print("  重新偵測已在進行中，跳過")
            return
        self._refresh_in_progress = True
        print("===== 重新偵測螢幕（完整重建） =====")

        # ── 第 0 步：先存檔，確保使用者的最新設定不會遺失 ──
        self.save_settings()

        # ── 第 1 步：停止所有分析器（主執行緒安全） ──
        self._for_each_screen_analyzer(lambda a: a.stop())
        self.screen_analyzers = []
        self.screen_analyzer = None
        self._monitor_auto_states = []

        # ── 第 2~4 步：背景執行緒執行 DDC 操作 ──
        self._refresh_worker = _RefreshDetectWorker(
            old_wrappers=list(self.monitor_wrappers),
        )
        self._refresh_worker.detection_complete.connect(self._on_refresh_complete)
        thread = QtCore.QThread(self)
        self._refresh_worker.moveToThread(thread)
        thread.started.connect(self._refresh_worker.run)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._refresh_thread = thread

    def _on_refresh_complete(self, result):
        """背景偵測完成後，在主執行緒更新 UI。
        合併當前 live 的遠端 wrappers（由 NetworkMonitorClient 管理），
        取代 worker 中捕捉的過時舊資料。"""
        self._refresh_in_progress = False

        if self._is_quitting:
            return

        if result.get("error"):
            print(f"  重新偵測失敗: {result['error']}")
            return

        fresh_wrappers = result.get("fresh_wrappers", [])
        detected_count = result.get("count", 0)

        self._prev_raw_monitor_count = detected_count

        # ── 設定 wrappers（worker 已保留舊範圍，但尚未套用孤兒降級） ──
        self.monitor_wrappers = list(fresh_wrappers)

        print(f"  完成: {len(fresh_wrappers)} 台可用螢幕, {len(self._remote_wrappers)} 台遠端")

        # ── 從設定檔載入範圍到 wrapper ──
        self._reload_monitor_ranges_from_settings()

        # ── 重建螢幕 UI（main page 的 monitor widget + settings page 的範圍設定） ──
        self._rebuild_monitor_widgets()
        self._rebuild_range_widgets()

        # ── 重建完成後，重新插入遠端螢幕 widgets ──
        self._reinsert_remote_widgets()

        # ── 重新初始化分析器（先重設 DXGI 狀態） ──
        _CaptureThread.reset_dxgi()
        self._init_screen_analyzers()
        self._sync_screen_analyzer_enabled()

        # ── 同步 UI 狀態 ──
        self._update_analyzer_levels()
        self.sync_ui_with_current_monitor_levels()
        self._update_auto_adjust_info()
        self._update_auto_formula_label()

        # 合併遠端 wrappers 到主列表
        self.monitor_wrappers.extend(self._remote_wrappers)

        self.refresh_tray_display()
        self.trigger_save()

        # 清理 thread reference
        if hasattr(self, "_refresh_thread") and self._refresh_thread is not None:
            self._refresh_thread.quit()
            self._refresh_thread = None

        print("===== 重新偵測完成 =====")

    def _rebuild_monitor_widgets(self):
        """重建 container 內的螢幕 widget（含遠端）。
        容器本身在 layout 中位置固定，內部變化不觸發外層 reflow。"""
        # 清理遠端 widget（C++ 物件 + 列表）
        if hasattr(self, "_remote_separator") and self._remote_separator is not None:
            try:
                self.monitor_container_layout.removeWidget(self._remote_separator)
                self._remote_separator.deleteLater()
            except Exception:
                pass
            self._remote_separator = None
        for w in list(self._remote_widgets):
            try:
                self.monitor_container_layout.removeWidget(w)
                w.deleteLater()
            except Exception:
                pass
        self._remote_widgets.clear()

        # 清理舊的 local widget
        for w in list(self.monitor_widgets):
            try:
                self.monitor_container_layout.removeWidget(w)
                w.deleteLater()
            except Exception:
                pass
        self.monitor_widgets.clear()

        # 填入新 local widget
        local_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]
        for wrapper in local_wrappers:
            widget = MonitorWidget(wrapper, self.threadpool)
            widget.value_changed.connect(self.on_monitor_link_changed)
            widget.set_available(wrapper.available)
            widget.set_ranges(wrapper.brightness_range, wrapper.contrast_range)
            self.monitor_widgets.append(widget)
            self.monitor_container_layout.addWidget(widget)

    def _rebuild_range_widgets(self):
        """重建 settings page → Monitors tab 中的範圍 widget。"""
        for rw in list(self.monitor_range_widgets):
            try:
                rw.deleteLater()
            except Exception:
                pass
        self.monitor_range_widgets.clear()

        # 找到 settings page → Monitors tab 的 layout
        try:
            tabs = None
            sl = self.settings_page.layout()
            if sl is not None:
                for i in range(sl.count()):
                    item = sl.itemAt(i)
                    if item and item.widget() and isinstance(item.widget(), QtWidgets.QTabWidget):
                        tabs = item.widget()
                        break
            if tabs is None:
                return
            mon_scroll = tabs.widget(1)  # Monitors tab index = 1
            if mon_scroll is None:
                return
            mon_container = mon_scroll.widget()
            if mon_container is None:
                return
            mon_layout = mon_container.layout()
            if mon_layout is None:
                return

            # 移除舊的 range widget + 「未偵測到」label
            for i in reversed(range(mon_layout.count())):
                item = mon_layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if w is None:
                    continue
                if isinstance(w, MonitorRangeWidget):
                    mon_layout.removeWidget(w)
                elif isinstance(w, QtWidgets.QLabel) and "未偵測" in w.text():
                    mon_layout.removeWidget(w)

            # 插入新 range widget（在 auto_group 之後、stretch 之前）
            insert_idx = mon_layout.count()
            for i in range(mon_layout.count()):
                item = mon_layout.itemAt(i)
                if item and item.spacerItem() is not None:
                    insert_idx = i
                    break

            for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
                if isinstance(wrapper, RemoteMonitorWrapper):
                    continue
                rw = MonitorRangeWidget(wrapper)
                rw.ranges_changed.connect(widget.set_ranges)
                rw.ranges_changed.connect(lambda _b, _c: self.trigger_save())
                rw.ranges_changed.connect(lambda _b, _c: self._update_analyzer_levels())
                self.monitor_range_widgets.append(rw)
                mon_layout.insertWidget(insert_idx, rw)
                insert_idx += 1
        except Exception:
            pass

    def _restore_shortcut_rows(self):
        """重建 settings page 後，從 self.level_shortcuts 重新填入快捷鍵行。
        兩條路徑（init / refresh）共用。"""
        # 先保存資料（clear_shortcut_rows 會觸發 on_level_shortcuts_changed
        # 從而清空 self.level_shortcuts）
        saved = list(self.level_shortcuts) or []
        self.clear_shortcut_rows()
        items = saved
        if not items:
            items = [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
        for shortcut_item in items:
            self.add_shortcut_row(shortcut_item)
        self.level_shortcuts = self.get_level_shortcuts()
        self.apply_level_shortcuts_to_hook()

    def _reload_monitor_ranges_from_settings(self):
        """從設定檔載入儲存的監視器範圍到 wrapper（widget 在建構時已自動讀取）。"""
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            monitors_data = saved.get("monitors", {})
            if not isinstance(monitors_data, dict):
                return

            range_map = self._build_range_map(monitors_data)

            for wrapper in self.monitor_wrappers:
                if isinstance(wrapper, RemoteMonitorWrapper):
                    continue
                pair = range_map.get(wrapper.name)
                if pair is None:
                    continue
                wrapper.brightness_range, wrapper.contrast_range = pair
        except Exception:
            pass

    def _build_range_map(self, monitors_data: dict) -> dict:
        """建立 saved_name → (b_range, c_range) 查詢表（純名稱配對）。"""
        range_map: dict = {}
        for saved_name, saved_data in monitors_data.items():
            if not isinstance(saved_data, dict):
                continue
            pair = (
                list(saved_data.get("b_range", [0, 100])),
                list(saved_data.get("c_range", [0, 100])),
            )
            range_map[saved_name] = pair
        return range_map

    # ---- 網路功能 ----
    def _sync_network_flags_from_mode(self):
        self._network_server_enabled = self._network_mode == "server"
        self._network_client_enabled = self._network_mode == "client"

    def _set_network_mode(self, mode, trigger_save=True):
        if mode not in ("disabled", "server", "client"):
            mode = "disabled"
        if mode == getattr(self, "_network_mode", "disabled"):
            self._sync_network_mode_controls()
            return

        self._network_mode = mode
        self._sync_network_flags_from_mode()

        if mode == "server":
            self._net_client.stop()
            self._clear_remote_wrappers()
            self._net_server.start()
        elif mode == "client":
            self._net_server.stop()
            self._net_client.start()
        else:
            self._net_server.stop()
            self._net_client.stop()
            self._clear_remote_wrappers()

        self._sync_network_mode_controls()
        if trigger_save:
            self.trigger_save()

    def _sync_network_mode_controls(self):
        if hasattr(self, "net_mode_combo"):
            index = {"disabled": 0, "server": 1, "client": 2}.get(self._network_mode, 0)
            self.net_mode_combo.blockSignals(True)
            self.net_mode_combo.setCurrentIndex(index)
            self.net_mode_combo.blockSignals(False)

    def _on_net_mode_changed(self, index):
        mode = {0: "disabled", 1: "server", 2: "client"}.get(int(index), "disabled")
        self._set_network_mode(mode)

    def _on_remote_monitors_updated(self, monitors):
        if hasattr(self, "net_servers_label"):
            server_count = len(set(m.get("_remote_server", "?") for m in monitors))
            self.net_servers_label.setText(f"已發現伺服器: {server_count} 台，共 {len(monitors)} 個螢幕")

        self.monitor_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]

        rebuild_needed = False
        seen_keys = set()
        for data in monitors:
            srv = data.get("_remote_name", "")
            name = data.get("name")
            key = (srv, name)
            seen_keys.add(key)
            existing = next((x for x in self._remote_wrappers if (x._server_name, x.name) == key), None)
            if existing:
                existing.update_from_data(data)
                widget = next((w for w in self._remote_widgets if w.monitor is existing), None)
                if widget:
                    self._sync_remote_widget(widget, existing)
                else:
                    widget = MonitorWidget(existing, self.threadpool)
                    self._sync_remote_widget(widget, existing)
                    widget.value_changed.connect(self._on_remote_monitor_link_changed)
                    self._remote_widgets.append(widget)
                    self.remote_servers_map[key] = widget
                    rebuild_needed = True
            else:
                wrapper = RemoteMonitorWrapper(data, srv)
                self._remote_wrappers.append(wrapper)
                widget = MonitorWidget(wrapper, self.threadpool)
                self._sync_remote_widget(widget, wrapper)
                widget.value_changed.connect(self._on_remote_monitor_link_changed)
                self._remote_widgets.append(widget)
                self.remote_servers_map[key] = widget
                rebuild_needed = True

        for wrapper in list(self._remote_wrappers):
            key = (wrapper._server_name, wrapper.name)
            if key in seen_keys:
                continue
            self._remote_wrappers.remove(wrapper)
            widget = next((w for w in self._remote_widgets if w.monitor is wrapper), None)
            if widget:
                self._remote_widgets.remove(widget)
                widget.deleteLater()
            self.remote_servers_map.pop(key, None)
            rebuild_needed = True

        if rebuild_needed:
            self._rebuild_remote_widgets()
        self._sync_global_link_from_available_monitors()
        self.refresh_tray_display()

    def _sync_remote_widget(self, widget, wrapper):
        try:
            brightness, contrast = wrapper.read_current_levels()
            if brightness is None:
                brightness = widget.b_slider.slider.value()
            if contrast is None:
                contrast = 0 if not wrapper.contrast_supported else widget.c_slider.slider.value()
            widget.sync_sliders(brightness, contrast)
            link_value = link_value_from_levels(wrapper, brightness, contrast)
            set_slider_object_value(widget.link_slider, link_value)
        except RuntimeError:
            # widget 已被刪除（UI 重建後遺留的舊參考），跳過
            pass

    def _broadcast_monitor_state_if_server_enabled(self):
        if not self._network_server_enabled or not getattr(self, "_net_server", None):
            return
        try:
            self._net_server.broadcast_monitor_state()
        except Exception as e:
            print(f"Broadcast monitor state error: {e}")

    def _network_monitor_snapshot(self):
        monitors = []
        for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
            if isinstance(wrapper, RemoteMonitorWrapper) or not getattr(wrapper, "available", False):
                continue
            brightness = widget.pending_brightness
            contrast = widget.pending_contrast
            if brightness is None:
                brightness = widget.b_slider.slider.value()
            if contrast is None:
                contrast = 0 if not getattr(wrapper, "contrast_supported", True) else widget.c_slider.slider.value()
            monitors.append({
                "name": wrapper.name,
                "brightness": int(round(brightness)) if brightness is not None else None,
                "contrast": int(round(contrast)) if contrast is not None else None,
                "brightness_range": wrapper.brightness_range,
                "contrast_range": wrapper.contrast_range,
                "brightness_supported": wrapper.brightness_supported,
                "contrast_supported": wrapper.contrast_supported,
            })
        return monitors

    def _network_app_state_snapshot(self):
        return {
            "auto_target": int(round(self._pending_network_auto_target if self._pending_network_auto_target is not None else self.auto_adjust_target)),
            "auto_enabled": bool(self._pending_network_auto_enabled if self._pending_network_auto_enabled is not None else self.auto_adjust_enabled),
        }

    def _remote_set_app_state(self, global_link, auto_target, auto_enabled):
        if auto_target is None and auto_enabled is None:
            return False
        if auto_target is not None:
            self._pending_network_auto_target = int(auto_target)
        if auto_enabled is not None:
            self._pending_network_auto_enabled = bool(auto_enabled)
        self.remote_state_applied.emit(global_link, auto_target, auto_enabled)
        return True

    def _on_remote_state_updated(self, state):
        if not isinstance(state, dict) or self._applying_remote_network_state:
            return
        auto_target = state.get("auto_target")
        auto_enabled = state.get("auto_enabled")
        if auto_target is None and auto_enabled is None:
            return
        if (
            (auto_target is None or int(auto_target) == int(round(self.auto_adjust_target)))
            and (auto_enabled is None or bool(auto_enabled) == bool(self.auto_adjust_enabled))
        ):
            return
        self._on_remote_state_applied(None, auto_target, auto_enabled)

    def _on_remote_state_applied(self, global_link, auto_target, auto_enabled):
        if self._applying_remote_network_state:
            return
        self._applying_remote_network_state = True
        try:
            if auto_enabled is not None and bool(auto_enabled) != bool(self.auto_adjust_enabled):
                self.on_auto_adjust_toggled(bool(auto_enabled))
            if auto_target is not None and int(auto_target) != int(round(self.auto_adjust_target)):
                self.set_auto_adjust_target(int(auto_target), trigger_save=False)
            if auto_enabled is not None:
                self._pending_network_auto_enabled = None
            if auto_target is not None:
                self._pending_network_auto_target = None
            self.trigger_save()
        finally:
            self._applying_remote_network_state = False

    def _sync_app_state_to_remote_servers(self, auto_target=None, auto_enabled=None):
        if self._applying_remote_network_state or not self._network_client_enabled:
            return
        for server_name in list(getattr(self._net_client, "_discovered_servers", {}).keys()):
            self._net_client.remote_set_state(server_name, auto_target=auto_target, auto_enabled=auto_enabled)

    def _on_remote_set_applied(self, name, brightness, contrast):
        for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
            if wrapper.name != name:
                continue
            b = widget.b_slider.slider.value() if brightness is None else int(brightness)
            c = widget.c_slider.slider.value() if contrast is None else int(contrast)
            if not getattr(wrapper, "contrast_supported", True):
                c = 0
            widget.pending_brightness = b
            widget.pending_contrast = c
            widget.sync_sliders(b, c)
            link_value = link_value_from_levels(wrapper, b, c)
            set_slider_object_value(widget.link_slider, link_value)
            widget.restart()
            break
        self._sync_global_link_from_available_monitors()
        self._update_auto_adjust_info()
        self.refresh_tray_display()
        self.trigger_save()
        self._broadcast_monitor_state_if_server_enabled()

    def _reinsert_remote_widgets(self):
        """UI 重建後，從現有 remote wrappers 重新建立 remote widget 並插入 container。"""
        for wrapper in list(self._remote_wrappers):
            key = (wrapper._server_name, wrapper.name)
            widget = MonitorWidget(wrapper, self.threadpool)
            self._sync_remote_widget(widget, wrapper)
            widget.value_changed.connect(self._on_remote_monitor_link_changed)
            self._remote_widgets.append(widget)
            self.remote_servers_map[key] = widget
        self._append_remote_widgets_to_container()

    def _append_remote_widgets_to_container(self):
        """將遠端 widget 附加到 monitor_container 底部（local 螢幕之後）。"""
        if not self._remote_widgets:
            return
        layout = getattr(self, "monitor_container_layout", None)
        if layout is None:
            return
        # 先移除舊的遠端 widget（避免重複）
        for w in self._remote_widgets:
            try:
                layout.removeWidget(w)
            except Exception:
                pass
        # 加入分隔標籤（若 container 中已有 local widget）
        if self.monitor_widgets:
            sep = QtWidgets.QLabel("─── 遠端螢幕 ───")
            sep.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color: gray; font-size: 10px; padding: 4px;")
            layout.addWidget(sep)
            self._remote_separator = sep
        # 插入遠端 widget
        for w in self._remote_widgets:
            layout.addWidget(w)

    def _clear_remote_wrappers(self):
        for w in self._remote_wrappers:
            if w in self.monitor_wrappers:
                self.monitor_wrappers.remove(w)
        self._remove_remote_from_container()
        for widget in self._remote_widgets:
            widget.deleteLater()
        self._remote_wrappers.clear()
        self._remote_widgets.clear()
        self.remote_servers_map.clear()

    def _remove_remote_from_container(self):
        """從 container 移除遠端 widget 與分隔線。"""
        layout = getattr(self, "monitor_container_layout", None)
        if layout is None:
            return
        sep = getattr(self, "_remote_separator", None)
        if sep is not None:
            try:
                layout.removeWidget(sep)
                sep.deleteLater()
            except Exception:
                pass
            self._remote_separator = None
        for w in list(self._remote_widgets):
            try:
                layout.removeWidget(w)
            except Exception:
                pass

    def _rebuild_remote_widgets(self):
        """重新整理 container 中的遠端 widget（位置在 local 螢幕之後）。"""
        self._remove_remote_from_container()
        self._append_remote_widgets_to_container()

    def _on_remote_monitor_link_changed(self, percent):
        """遠端螢幕聯動滑桿變更 → 透過網路發送 set 指令"""
        widget = self.sender()
        if not isinstance(widget, MonitorWidget):
            return
        wrapper = widget.monitor
        if not isinstance(wrapper, RemoteMonitorWrapper):
            return
        srv = wrapper._server_name
        if not wrapper.contrast_supported:
            brightness, _contrast = levels_from_link_value(wrapper, percent, unsupported_contrast=None)
            self._queue_remote_set(srv, wrapper.name, brightness, None, wrapper)
            self._sync_global_link_from_available_monitors()
            self._sync_main_global_link_controls()
            self.refresh_tray_display()
            return
        brightness, contrast = levels_from_link_value(wrapper, percent)
        self._queue_remote_set(srv, wrapper.name, brightness, contrast, wrapper)
        self._sync_global_link_from_available_monitors()
        self._sync_main_global_link_controls()
        self.refresh_tray_display()

    def _queue_remote_set(self, server_name, monitor_name, brightness, contrast, wrapper):
        key = (server_name, monitor_name)
        self._pending_remote_sets[key] = {
            "brightness": brightness,
            "contrast": contrast,
            "wrapper": wrapper,
            "scheduled": self._pending_remote_sets.get(key, {}).get("scheduled", False),
        }
        if self._pending_remote_sets[key]["scheduled"]:
            return
        self._pending_remote_sets[key]["scheduled"] = True
        QtCore.QTimer.singleShot(100, lambda k=key: self._flush_remote_set(k))

    def _flush_remote_set(self, key):
        payload = self._pending_remote_sets.pop(key, None)
        if not payload:
            return
        server_name, monitor_name = key
        brightness = payload.get("brightness")
        contrast = payload.get("contrast")
        wrapper = payload.get("wrapper")
        if self._net_client.remote_set(server_name, monitor_name, brightness, contrast) and isinstance(wrapper, RemoteMonitorWrapper):
            wrapper._brightness = brightness
            wrapper._contrast = 0 if contrast is None and not wrapper.contrast_supported else contrast

    def _remote_set_monitor(self, name, brightness, contrast):
        """由 NetworkMonitorServer 回呼：遠端要求設定本機螢幕"""
        for w in self.monitor_wrappers:
            if w.name == name and w.available:
                if brightness is None and contrast is None:
                    return False
                if contrast is not None and not getattr(w, "contrast_supported", True):
                    contrast = 0
                self.remote_set_applied.emit(name, brightness, contrast)
                return True
        return False

    def show_settings_page(self):
        self.stack.setCurrentWidget(self.settings_page)
        self.show()
        self.raise_()
        self.activateWindow()

    def init_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self)
        self.tray_menu = QtWidgets.QMenu()
        
        action_main = self.tray_menu.addAction("顯示主設定 (Main Settings)")
        action_main.triggered.connect(self.show_main_page)
        self.tray_menu.addSeparator()
        
        action_quit = self.tray_menu.addAction("完全退出 (Quit)")
        action_quit.triggered.connect(self.quit_app)
        
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        
        # 啟動時僅初始化圖示，避免主動下發 DDC 指令
        self.refresh_tray_display()
        self.tray.show()

    def sync_ui_with_current_monitor_levels(self):
        if not self.monitor_wrappers or not self.monitor_widgets:
            return
        if len(self.monitor_wrappers) != len(self.monitor_widgets):
            return

        link_values = []
        for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
            try:
                brightness, contrast = wrapper.read_current_levels()
                if brightness is None:
                    brightness = widget.b_slider.slider.value()
                if contrast is None:
                    contrast = widget.c_slider.slider.value()

                widget.sync_sliders(brightness, contrast)

                link_value = link_value_from_levels(wrapper, brightness, contrast)
                set_slider_object_value(widget.link_slider, link_value)
                link_values.append(link_value)
            except RuntimeError:
                # widget 已被刪除，跳過
                pass

        self._sync_global_link_from_available_monitors()
        self._update_auto_adjust_info()
        self._sync_main_global_link_controls()
        self.refresh_tray_display()

    def init_global_hook(self):
        if sys.platform != "win32":
            return
        try:
            self.global_hook = GlobalHotkeyWheelHook(self)
            self.global_hook.step_requested.connect(self.on_global_hook_step)
            self.global_hook.level_requested.connect(self.on_global_hook_level)
            self.global_hook.toggle_auto_requested.connect(self.on_global_hook_toggle_auto)
            self.apply_shortcut_to_hook()
            self.apply_level_shortcuts_to_hook()
            self.global_hook.start()
        except Exception as e:
            print("Global hook init error:", e)

    def on_auto_adjust_toggled(self, checked):
        self.auto_adjust_enabled = bool(checked)
        self._sync_screen_analyzer_enabled()
        self._for_each_screen_analyzer(lambda analyzer: analyzer.reset_dynamic_capture_interval())
        # 同步主介面與設定頁的 checkbox
        for cb in [
            getattr(self, "auto_adjust_checkbox", None),
            getattr(self, "main_auto_adjust_checkbox", None),
        ]:
            if cb is not None and cb.isChecked() != checked:
                cb.blockSignals(True)
                cb.setChecked(checked)
                cb.blockSignals(False)
        self.refresh_tray_display()
        self.trigger_save()
        self._broadcast_monitor_state_if_server_enabled()
        self._sync_app_state_to_remote_servers(auto_enabled=self.auto_adjust_enabled)

    def on_auto_adjust_settings_changed(self):
        self.auto_adjust_k = float(self.auto_adjust_k_spin.value())
        self.auto_adjust_weight = float(self.auto_adjust_weight_spin.value())
        self.auto_adjust_capture_interval = float(self.auto_adjust_capture_interval_spin.value())
        self.auto_adjust_tick_interval = int(self.auto_adjust_tick_interval_spin.value())
        self.auto_adjust_resource_saving_enabled = bool(self.auto_adjust_resource_saving_checkbox.isChecked())
        self.auto_adjust_resource_saving_idle_seconds = float(self.auto_adjust_resource_saving_idle_spin.value())
        self._for_each_screen_analyzer(self._configure_screen_analyzer)
        self._sync_screen_analyzer_enabled()
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(self.auto_adjust_target)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(self.auto_adjust_target))
        self._update_auto_formula_label()
        self._update_auto_adjust_info()
        self.trigger_save()

    def on_main_target_slider_changed(self, value):
        self.set_auto_adjust_target(value)

    def on_main_global_link_slider_changed(self, value):
        self.set_global_link(value)

    def _sync_main_global_link_controls(self):
        if hasattr(self, "main_global_link_slider"):
            value = int(round(self.global_link_value))
            self.main_global_link_slider.blockSignals(True)
            self.main_global_link_slider.setValue(value)
            self.main_global_link_slider.blockSignals(False)
            self.main_global_link_value_label.setText(str(value))

    def _available_global_link_values(self):
        values = []
        for idx, (wrapper, widget) in enumerate(zip(self.monitor_wrappers, self.monitor_widgets)):
            if not getattr(wrapper, "available", False):
                continue
            try:
                values.append(("local", idx, int(widget.link_slider.slider.value())))
            except RuntimeError:
                pass
        for idx, (wrapper, widget) in enumerate(zip(self._remote_wrappers, self._remote_widgets)):
            if not getattr(wrapper, "available", False):
                continue
            try:
                values.append(("remote", idx, int(widget.link_slider.slider.value())))
            except RuntimeError:
                pass
        return values

    def _sync_global_link_from_available_monitors(self):
        values = self._available_global_link_values()
        if not values:
            return
        self.global_link_value = int(round(sum(value for _kind, _idx, value in values) / len(values)))
        for kind, idx, value in values:
            if kind == "local" and idx < len(self.screen_analyzers) and self.screen_analyzers[idx] is not None:
                self.screen_analyzers[idx].set_current_ddc(value)
        self._sync_main_global_link_controls()

    def set_auto_adjust_target(self, value, trigger_save=True):
        value = max(0, min(100, self.snap_to_step(value)))
        value = int(round(value))
        self.auto_adjust_target = value
        # 強制同步 analyzer 內部狀態與實際 slider 值，避免手動 step 在調整中用舊基數計算
        for idx, analyzer in enumerate(getattr(self, "screen_analyzers", [])):
            if analyzer is None:
                continue
            if idx < len(self.monitor_widgets):
                analyzer.set_current_ddc(self.monitor_widgets[idx].link_slider.slider.value(), force=True)
        self._for_each_screen_analyzer(lambda a: setattr(a, "target", value))
        self._for_each_screen_analyzer(lambda a: a.recalculate_desired_from_last_luminance())
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(value)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(value))
        self._update_auto_adjust_info()
        if trigger_save:
            self.trigger_save()
        self._broadcast_monitor_state_if_server_enabled()
        self._sync_app_state_to_remote_servers(auto_target=value)

    def adjust_auto_adjust_target(self, delta):
        new_val = self.snap_to_step(self.auto_adjust_target + delta)
        self.set_auto_adjust_target(new_val)

    def _update_analyzer_levels(self):
        """ 計算所有螢幕亮度+對比總級數並通知 ScreenAnalyzer """
        total = sum(
            (w.brightness_range[1] - w.brightness_range[0]) +
            ((w.contrast_range[1] - w.contrast_range[0]) if getattr(w, "contrast_supported", True) else 0)
            for w in self.monitor_wrappers
        )
        self._for_each_screen_analyzer(lambda analyzer: setattr(analyzer, "total_levels", max(1, total)))

    def on_screen_adjust_requested(self, monitor_index, delta_percent):
        if not self.monitor_wrappers or not self.monitor_widgets:
            return
        if len(self.monitor_wrappers) != len(self.monitor_widgets):
            return
        if monitor_index < 0 or monitor_index >= len(self.monitor_widgets):
            return

        try:
            delta_percent = float(delta_percent)
        except (TypeError, ValueError):
            return
        if abs(delta_percent) <= 1e-9:
            return

        sign = 1 if delta_percent > 0 else -1
        abs_percent = abs(delta_percent)

        wrapper = self.monitor_wrappers[monitor_index]
        widget = self.monitor_widgets[monitor_index]
        if not wrapper.available or isinstance(wrapper, RemoteMonitorWrapper):
            return
        try:
            b_min, b_max = wrapper.brightness_range
            c_min, c_max = wrapper.contrast_range
            b_range = max(0, b_max - b_min)
            contrast_supported = getattr(wrapper, "contrast_supported", True)
            c_range = max(0, c_max - c_min) if contrast_supported else 0
            total_levels = b_range + c_range
            if total_levels <= 0:
                return

            level_step = int(round(total_levels * (abs_percent / 100.0)))
            if level_step <= 0:
                level_step = 1

            brightness = int(widget.b_slider.slider.value())
            contrast = 0 if not contrast_supported else int(widget.c_slider.slider.value())
            brightness = max(b_min, min(b_max, brightness))
            contrast = 0 if not contrast_supported else max(c_min, min(c_max, contrast))

            if not contrast_supported:
                current_units = max(0, min(b_range, brightness - b_min))
            elif brightness <= b_min:
                current_units = max(0, min(c_range, contrast - c_min))
            else:
                current_units = c_range + max(0, min(b_range, brightness - b_min))

            new_units = max(0, min(total_levels, current_units + sign * level_step))

            link_value = int(round((new_units / total_levels) * 100))
            new_brightness, new_contrast = levels_from_link_units(wrapper, new_units)
            set_slider_object_value(widget.link_slider, link_value)

            widget.pending_brightness = int(round(new_brightness))
            widget.pending_contrast = int(round(new_contrast))
            widget.sync_sliders(widget.pending_brightness, widget.pending_contrast)
            widget.restart()
        except RuntimeError:
            return

        self._sync_global_link_from_available_monitors()
        if monitor_index < len(self.screen_analyzers) and self.screen_analyzers[monitor_index] is not None:
            self.screen_analyzers[monitor_index].set_current_ddc(link_value)
        self._update_auto_adjust_info(monitor_index)
        self._broadcast_monitor_state_if_server_enabled()

    def on_luminance_updated(self, monitor_index, lum):
        if 0 <= monitor_index < len(self._monitor_auto_states):
            self._monitor_auto_states[monitor_index]["avg"] = float(lum)
        self._update_auto_adjust_info(monitor_index)

    def _on_luminance_source_updated(self, monitor_index, source: str):
        if 0 <= monitor_index < len(self._monitor_auto_states):
            self._monitor_auto_states[monitor_index]["source"] = source
        self._update_auto_adjust_info(monitor_index)

    def _current_content_coeff(self):
        values = [
            float(state["avg"])
            for state in self._monitor_auto_states
            if state.get("avg") is not None
        ]
        if not values:
            return float(AUTO_BRIGHTNESS_CONTENT_COEFF)
        return get_dynamic_content_coeff(sum(values) / len(values))

    def _update_auto_formula_label(self):
        label = getattr(self, "auto_formula_label", None)
        if label is None:
            return
        content_coeff = self._current_content_coeff()
        weight = float(self.auto_adjust_weight)
        label.setText(
            "加權亮度 = "
            f"(<i>畫面亮度</i> &times; <i>內容係數({content_coeff:.2f})</i> + "
            f"<i>背光亮度</i> &times; <i>背光權重({weight:.2f})</i>) "
            f"&divide; (<i>內容係數({content_coeff:.2f})</i> + <i>背光權重({weight:.2f})</i>)"
        )

    def _update_auto_adjust_info(self, monitor_index=None):
        self._update_auto_formula_label()
        indexes = range(len(self.monitor_widgets)) if monitor_index is None else [monitor_index]
        target = float(self.auto_adjust_target)
        weight = float(self.auto_adjust_weight)
        for idx in indexes:
            if idx < 0 or idx >= len(self.monitor_widgets):
                continue
            state = self._monitor_auto_states[idx] if idx < len(self._monitor_auto_states) else {"avg": None, "source": "—", "current": None}
            avg = state.get("avg")
            source = state.get("source", "—")
            backlight = float(self.monitor_widgets[idx].link_slider.slider.value())
            if avg is None:
                state["current"] = None
                text = f"畫面亮度: -- | 背光亮度: {backlight:.1f}% | 加權亮度: -- | 目標亮度: {target:.1f}% | 權重: {weight:.2f} | 來源: {source}"
            else:
                c = get_dynamic_content_coeff(avg)
                current = (avg * c + backlight * weight) / (c + weight)
                state["current"] = current
                text = f"畫面亮度: {avg:.1f}% | 背光亮度: {backlight:.1f}% | 加權亮度: {current:.1f}% | 目標亮度: {target:.1f}% | 權重: {weight:.2f} | 來源: {source}"
            self.monitor_widgets[idx].set_auto_info(text)

        currents = [state.get("current") for state in self._monitor_auto_states if state.get("current") is not None]
        self.current_effective_brightness = sum(currents) / len(currents) if currents else None
        self.refresh_tray_display()

    def on_step_changed(self, text):
        self.step_value = float(text)
        self.trigger_save()

    def on_shortcut_changed(self, _):
        self.shortcut_key1 = self.shortcut_key1_button.key_name
        self.shortcut_key2 = self.shortcut_key2_button.key_name
        self.shortcut_key3 = self.shortcut_key3_button.key_name
        self.apply_shortcut_to_hook()
        self.trigger_save()

    def apply_shortcut_to_hook(self):
        if self.global_hook is not None:
            self.global_hook.set_trigger_shortcut(self.shortcut_key1, self.shortcut_key2, self.shortcut_key3)

    def add_shortcut_row(self, shortcut=None):
        row = ShortcutConfigRow(shortcut)
        row.changed.connect(self.on_level_shortcuts_changed)
        row.remove_requested.connect(self.remove_shortcut_row)
        self.shortcut_rows.append(row)
        self.shortcut_rows_layout.addWidget(row)
        self.on_level_shortcuts_changed()

    def remove_shortcut_row(self, row):
        if row in self.shortcut_rows:
            self.shortcut_rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self.on_level_shortcuts_changed()

    def clear_shortcut_rows(self):
        for row in list(self.shortcut_rows):
            self.remove_shortcut_row(row)

    def get_level_shortcuts(self):
        return [row.get_data() for row in self.shortcut_rows]

    def on_level_shortcuts_changed(self):
        self.level_shortcuts = self.get_level_shortcuts()
        self.apply_level_shortcuts_to_hook()
        self.trigger_save()

    def apply_level_shortcuts_to_hook(self):
        if self.global_hook is not None:
            self.global_hook.set_level_shortcuts(self.level_shortcuts)

    def get_step_value(self):
        if hasattr(self, "step_combo"):
            return float(self.step_combo.currentText())
        return float(self.step_value)

    def snap_to_step(self, value):
        """將數值對齊到 step 的整數倍（從 0 開始累加）。"""
        step = self.get_step_value()
        if step <= 0:
            return round(value)
        return round(value / step) * step

    def get_startup_command(self):
        python_path = os.path.abspath(sys.executable)
        script_path = os.path.abspath(__file__)
        return f'"{python_path}" "{script_path}"'

    def is_startup_enabled(self):
        if sys.platform != "win32" or winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.STARTUP_REG_PATH, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, self.STARTUP_VALUE_NAME)
                return bool(value)
        except FileNotFoundError:
            return False
        except Exception as e:
            print("Read startup setting error:", e)
            return False

    def set_startup_enabled(self, enabled):
        if sys.platform != "win32" or winreg is None:
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, self.STARTUP_VALUE_NAME, 0, winreg.REG_SZ, self.get_startup_command())
                else:
                    try:
                        winreg.DeleteValue(key, self.STARTUP_VALUE_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            print("Set startup setting error:", e)

    def on_autostart_toggled(self, checked):
        self.auto_start_enabled = bool(checked)
        self.set_startup_enabled(self.auto_start_enabled)
        self.trigger_save()

    def on_network_debug_toggled(self, checked):
        global NETWORK_DEBUG_LOG_ENABLED
        self.network_debug_enabled = bool(checked)
        NETWORK_DEBUG_LOG_ENABLED = self.network_debug_enabled
        self.trigger_save()

    def create_tray_icon(self, current_value, target_value=None):
        """ 動態繪製托盤圖示；啟用自動亮度時顯示上下兩行數值 """
        pixmap = QtGui.QPixmap(32, 32)
        pixmap.fill(QtGui.QColor("transparent"))
        painter = QtGui.QPainter(pixmap)

        painter.setPen(QtGui.QColor("white"))
        current_text = str(int(round(current_value)))
        target_text = str(int(round(target_value if target_value is not None else current_value)))

        if self.auto_adjust_enabled:
            top_font = painter.font()
            top_font.setPixelSize(18 if len(current_text) <= 2 else 16)
            top_font.setBold(True)
            painter.setFont(top_font)
            painter.drawText(QtCore.QRect(0, 0, 32, 16), int(QtCore.Qt.AlignmentFlag.AlignCenter), current_text)

            bottom_font = painter.font()
            bottom_font.setPixelSize(18 if len(target_text) <= 2 else 16)
            bottom_font.setBold(True)
            painter.setFont(bottom_font)
            painter.drawText(QtCore.QRect(0, 16, 32, 16), int(QtCore.Qt.AlignmentFlag.AlignCenter), target_text)
        else:
            font = painter.font()
            font.setPixelSize(28 if len(current_text) <= 2 else 20)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(pixmap.rect(), int(QtCore.Qt.AlignmentFlag.AlignCenter), current_text)
        painter.end()
        
        return QtGui.QIcon(pixmap)

    def refresh_tray_display(self):
        current = float(self.global_link_value)
        target = float(self.auto_adjust_target) if self.auto_adjust_enabled else None
        self.tray.setIcon(self.create_tray_icon(current, target))
        if self.auto_adjust_enabled:
            effective = getattr(self, "current_effective_brightness", None)
            effective_text = "--" if effective is None else f"{float(effective):.1f}%"
            self.tray.setToolTip(
                f"背光亮度: {self.global_link_value:.1f}%\n"
                f"當前亮度: {effective_text}\n"
                f"目標亮度: {self.auto_adjust_target:.1f}%"
            )
        else:
            self.tray.setToolTip(f"全域聯動: {self.global_link_value}%")

    def on_global_hook_step(self, delta):
        step = delta * self.get_step_value()
        # 先直接調整亮度（固定 step，不經 analyzer）
        self.adjust_global_link(step)
        # 自動模式下同步更新目標值
        if self.auto_adjust_enabled:
            new_target = max(0, min(100, self.snap_to_step(self.auto_adjust_target + step)))
            self.set_auto_adjust_target(new_target, trigger_save=False)

    def on_global_hook_level(self, value):
        # 絕對值快捷鍵：不管模式，同時更新目標亮度與背光亮度
        self.set_auto_adjust_target(value, trigger_save=False)
        self.set_global_link(value)

    def on_global_hook_toggle_auto(self):
        # 切換自動亮度開關
        self.on_auto_adjust_toggled(not self.auto_adjust_enabled)

    def adjust_global_link(self, delta):
        new_value = max(0, min(100, self.snap_to_step(self.global_link_value + delta)))
        if new_value == self.global_link_value:
            return
        self.set_global_link(new_value)

    def set_global_link(self, value):
        self.update_global_link(value)
        self.trigger_save()

    def on_tray_activated(self, reason):
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_main_page()

    def on_monitor_link_changed(self, value):
        if self._updating_global_link:
            return
        self._sync_global_link_from_available_monitors()
        self._update_auto_adjust_info()
        self.trigger_save()
        self._broadcast_monitor_state_if_server_enabled()

    def update_global_link(self, value):
        """ 統一更新介面、圖示與所有本機螢幕的聯動值（不控制遠端螢幕）。 """
        if self._updating_global_link:
            return

        value = int(self.snap_to_step(value))

        self._updating_global_link = True
        try:
            for wrapper, w in zip(self.monitor_wrappers, self.monitor_widgets):
                if not getattr(wrapper, "available", False) or isinstance(wrapper, RemoteMonitorWrapper):
                    continue
                try:
                    set_slider_object_value(w.link_slider, value)
                    w.on_link(value)
                except RuntimeError:
                    pass
            self._sync_global_link_from_available_monitors()
            self._update_auto_adjust_info()
            self._broadcast_monitor_state_if_server_enabled()
        finally:
            self._updating_global_link = False

    def closeEvent(self, event):
        if not self._is_quitting:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def quit_app(self):
        self.save_settings()
        if self.global_hook is not None:
            self.global_hook.stop()
        if hasattr(self, "_hotplug_watcher"):
            self._hotplug_watcher.stop()
        if hasattr(self, "_hotplug_debounce_timer"):
            self._hotplug_debounce_timer.stop()
        if hasattr(self, "_refresh_thread") and self._refresh_thread is not None:
            self._refresh_thread.quit()
            self._refresh_thread = None
        self._for_each_screen_analyzer(lambda analyzer: analyzer.stop())
        self._net_server.stop()
        self._net_client.stop()
        self._is_quitting = True
        QtWidgets.QApplication.quit()

    def trigger_save(self):
        if self._loading_settings:
            return
        # 重新計時，延遲 500ms 後才執行寫入，避免拉動滑桿時瘋狂存檔
        self.save_timer.start(200)

    def save_settings(self):
        # 直接從 monitor_wrappers 推導有效名稱（不依賴 _known_monitor_names 變數）
        local_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]
        valid_names = sorted(
            w.name for w in local_wrappers if _is_valid_monitor_name(w.name)
        )
        data = {
            "known_monitor_names": valid_names,
            "global_link": self.global_link_value,
            "step": self.get_step_value(),
            "auto_start": self.auto_start_enabled,
            "network_debug": self.network_debug_enabled,
            "shortcut": {
                "key1": self.shortcut_key1,
                "key2": self.shortcut_key2,
                "key3": self.shortcut_key3,
            },
            "level_shortcuts": self.level_shortcuts,
            "auto_adjust": {
                "enabled": self.auto_adjust_enabled,
                "target": self.auto_adjust_target,
                "k": self.auto_adjust_k,
                "weight": self.auto_adjust_weight,
                "capture_interval": self.auto_adjust_capture_interval,
                "tick_interval": self.auto_adjust_tick_interval,
                "resource_saving_enabled": self.auto_adjust_resource_saving_enabled,
                "resource_saving_idle_seconds": self.auto_adjust_resource_saving_idle_seconds,
            },
            "network": {
                "mode": self._network_mode,
                "server_enabled": self._network_server_enabled,
                "client_enabled": self._network_client_enabled,
            },
            "monitors": {},
        }
        # 以 name 為 key 儲存監視器範圍資料
        # 先從舊檔案載入所有已知設定，再用當前偵測到的資料覆蓋（保留舊名稱設定）
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            old_monitors = old_data.get("monitors", {})
            if isinstance(old_monitors, dict):
                data["monitors"] = dict(old_monitors)
        except Exception:
            pass

        for wrapper in local_wrappers:
            name = wrapper.name
            if _is_valid_monitor_name(name):
                data["monitors"][name] = {
                    "b_range": wrapper.brightness_range,
                    "c_range": wrapper.contrast_range
                }

        try:
            tmp = SETTINGS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SETTINGS_PATH)  # 原子操作
        except Exception as e:
            print("Save Error:", e)

    def load_settings(self):
        try:
            self._loading_settings = True
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            # 相容舊版資料格式或讀取新版 global_link
            saved_link = data.get("global_link", 0) if isinstance(data, dict) else 0
            saved_step = data.get("step", 5) if isinstance(data, dict) else 5
            saved_auto_start = data.get("auto_start", self.is_startup_enabled()) if isinstance(data, dict) else self.is_startup_enabled()
            saved_network_debug = bool(data.get("network_debug", False)) if isinstance(data, dict) else False
            shortcut = data.get("shortcut", {}) if isinstance(data, dict) else {}
            saved_level_shortcuts = data.get("level_shortcuts", [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]) if isinstance(data, dict) else [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
            saved_trigger_keys = shortcut.get("keys") if isinstance(shortcut, dict) else None
            if isinstance(saved_trigger_keys, list):
                saved_trigger_keys = list(saved_trigger_keys[:3])
            else:
                saved_trigger_keys = [
                    shortcut.get("key1", "Alt") if isinstance(shortcut, dict) else "Alt",
                    shortcut.get("key2", "Win") if isinstance(shortcut, dict) else "Win",
                    shortcut.get("key3", "None") if isinstance(shortcut, dict) else "None",
                ]
            while len(saved_trigger_keys) < 3:
                saved_trigger_keys.append("None")
            monitors_data = data.get("monitors", data) if isinstance(data, dict) else data
            auto_adjust_data = data.get("auto_adjust", {}) if isinstance(data, dict) else {}
            net_data = data.get("network", {}) if isinstance(data, dict) else {}

            # 載入網路功能設定（先存值，等 UI 建立後再啟動 server/client）
            loaded_network_mode = net_data.get("mode") if isinstance(net_data, dict) else None
            if loaded_network_mode not in ("disabled", "server", "client"):
                if bool(net_data.get("server_enabled", False)):
                    loaded_network_mode = "server"
                elif bool(net_data.get("client_enabled", False)):
                    loaded_network_mode = "client"
                else:
                    loaded_network_mode = "disabled"
            self._network_mode = loaded_network_mode
            self._sync_network_flags_from_mode()
            self.network_debug_enabled = saved_network_debug
            global NETWORK_DEBUG_LOG_ENABLED
            NETWORK_DEBUG_LOG_ENABLED = self.network_debug_enabled

            # 先決定是否啟用自動調整，避免啟動流程誤下發 DDC 指令
            self.auto_adjust_enabled = bool(auto_adjust_data.get("enabled", False))

            # 載入 Step 與快捷鍵
            saved_step = float(saved_step)
            STEP_OPTIONS_LOAD = ["1", "2", "2.5", "4", "5","10"]
            step_text = str(saved_step)
            if step_text not in STEP_OPTIONS_LOAD:
                # 相容舊版整數 step：取最接近值
                best = min(STEP_OPTIONS_LOAD, key=lambda s: abs(float(s) - saved_step))
                step_text = best
            self.step_combo.blockSignals(True)
            self.step_combo.setCurrentText(step_text)
            self.step_combo.blockSignals(False)
            self.step_value = float(self.step_combo.currentText())

            saved_trigger_keys = [
                key if key in KEY_NAME_TO_VKS or key == "None" else "None"
                for key in saved_trigger_keys
            ]

            for button, key in zip(
                [self.shortcut_key1_button, self.shortcut_key2_button, self.shortcut_key3_button],
                saved_trigger_keys,
            ):
                button.blockSignals(True)
                button.set_key_name(key)
                button.blockSignals(False)

            self.shortcut_key1, self.shortcut_key2, self.shortcut_key3 = saved_trigger_keys
            self.apply_shortcut_to_hook()

            # 載入快捷鍵（共用方法，與 refresh 路徑一致）
            self.level_shortcuts = [dict(item) for item in saved_level_shortcuts]
            self._restore_shortcut_rows()

            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(bool(saved_auto_start))
            self.autostart_checkbox.blockSignals(False)
            self.auto_start_enabled = bool(saved_auto_start)
            self.set_startup_enabled(self.auto_start_enabled)

            # 載入監視器範圍 — 精確名稱配對
            if isinstance(monitors_data, dict):
                local = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]
                range_map = self._build_range_map(monitors_data)
                for wrapper, widget, rw in zip(local, self.monitor_widgets, self.monitor_range_widgets):
                    pair = range_map.get(wrapper.name)
                    if pair is None:
                        continue
                    b_range, c_range = pair
                    wrapper.brightness_range = b_range
                    wrapper.contrast_range = c_range
                    rw.set_ranges(b_range, c_range, emit_signal=False)
                    widget.set_ranges(b_range, c_range)

            # 載入畫面自動調整設定
            self.auto_adjust_target = int(auto_adjust_data.get("target", 50))
            self.auto_adjust_k = float(auto_adjust_data.get("k", 0.8))
            # 相容舊版 threshold — 不再使用，保留讀取避免警告
            _old_threshold = auto_adjust_data.get("threshold", None)
            self.auto_adjust_weight = float(auto_adjust_data.get("weight", AUTO_BRIGHTNESS_WEIGHT_DEFAULT))
            self.auto_adjust_capture_interval = float(auto_adjust_data.get("capture_interval", 1.0))
            self.auto_adjust_tick_interval = int(auto_adjust_data.get("tick_interval", 200))
            self.auto_adjust_resource_saving_enabled = bool(auto_adjust_data.get("resource_saving_enabled", True))
            self.auto_adjust_resource_saving_idle_seconds = float(auto_adjust_data.get("resource_saving_idle_seconds", 5.0))
            self.auto_adjust_capture_interval = max(0.1, min(5.0, self.auto_adjust_capture_interval))
            self.auto_adjust_k = max(0.1, min(5.0, self.auto_adjust_k))
            self.auto_adjust_resource_saving_idle_seconds = max(0.1, min(60.0, self.auto_adjust_resource_saving_idle_seconds))
            if hasattr(self, "network_debug_checkbox"):
                self.network_debug_checkbox.blockSignals(True)
                self.network_debug_checkbox.setChecked(self.network_debug_enabled)
                self.network_debug_checkbox.blockSignals(False)
            self.auto_adjust_checkbox.blockSignals(True)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.blockSignals(True)
            self.auto_adjust_k_spin.blockSignals(True)
            self.auto_adjust_weight_spin.blockSignals(True)
            self.auto_adjust_capture_interval_spin.blockSignals(True)
            self.auto_adjust_tick_interval_spin.blockSignals(True)
            self.auto_adjust_resource_saving_checkbox.blockSignals(True)
            self.auto_adjust_resource_saving_idle_spin.blockSignals(True)
            self.auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            self.auto_adjust_k_spin.setValue(self.auto_adjust_k)
            self.auto_adjust_weight_spin.setValue(self.auto_adjust_weight)
            self.auto_adjust_capture_interval_spin.setValue(self.auto_adjust_capture_interval)
            self.auto_adjust_tick_interval_spin.setValue(self.auto_adjust_tick_interval)
            self.auto_adjust_resource_saving_checkbox.setChecked(self.auto_adjust_resource_saving_enabled)
            self.auto_adjust_resource_saving_idle_spin.setValue(self.auto_adjust_resource_saving_idle_seconds)
            self.auto_adjust_checkbox.blockSignals(False)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.blockSignals(False)
            self.auto_adjust_k_spin.blockSignals(False)
            self.auto_adjust_weight_spin.blockSignals(False)
            self.auto_adjust_capture_interval_spin.blockSignals(False)
            self.auto_adjust_tick_interval_spin.blockSignals(False)
            self.auto_adjust_resource_saving_checkbox.blockSignals(False)
            self.auto_adjust_resource_saving_idle_spin.blockSignals(False)
            self.set_auto_adjust_target(self.auto_adjust_target, trigger_save=False)
            self._for_each_screen_analyzer(self._configure_screen_analyzer)
            self._sync_screen_analyzer_enabled()

            # 啟動時：未勾選自動調整則只讀取當前螢幕值更新 UI，不主動寫入 DDC。
            if self.auto_adjust_enabled:
                self.update_global_link(saved_link)
            else:
                self.sync_ui_with_current_monitor_levels()

            self._update_auto_adjust_info()
        except FileNotFoundError:
            self.clear_shortcut_rows()
            for shortcut_item in DEFAULT_LEVEL_SHORTCUTS:
                self.add_shortcut_row(shortcut_item)
            self.level_shortcuts = self.get_level_shortcuts()
            self.apply_level_shortcuts_to_hook()
            self.sync_ui_with_current_monitor_levels()
            self.refresh_tray_display()
            # 首次啟動時立即建立預設設定檔，避免使用者找不到檔案。
            self.save_settings()
        except Exception as e:
            print("Load Error:", e)
            # 檔案可能損毀 → 備份後建立新檔，避免用預設值覆蓋
            try:
                import shutil
                bak = SETTINGS_PATH + ".bak"
                shutil.copy2(SETTINGS_PATH, bak)
                print(f"已備份損毀設定檔至 {bak}")
            except Exception:
                pass
        finally:
            self._loading_settings = False

        # 啟動網路功能（需在 load_settings 完成後，確保 UI 控制項已存在）
        loaded_network_mode = self._network_mode
        self._network_mode = "disabled"
        self._sync_network_flags_from_mode()
        self._set_network_mode(loaded_network_mode, trigger_save=False)


# =========================
# Main
# =========================
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
