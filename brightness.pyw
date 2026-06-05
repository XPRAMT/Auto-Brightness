import sys
import json
import threading
import ctypes
import os
import time
import socket
from ctypes import wintypes

# 避免 Windows 上 Qt 輸出 DPI awareness 的無害警告
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

from PyQt6 import QtWidgets, QtCore, QtGui
from monitorcontrol import get_monitors

# zeroconf 用於 mDNS
try:
    from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser
except Exception:
    Zeroconf = None
    ServiceInfo = None
    ServiceBrowser = None

try:
    import winreg
except ImportError:
    winreg = None

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# 嘗試導入 DXGI 截圖模組
try:
    import dxcam
    HAS_DXGI = True
except ImportError:
    HAS_DXGI = False

try:
    import vapoursynth as vs
    HAS_VAPOURSYNTH = True
except ImportError:
    HAS_VAPOURSYNTH = False

try:
    import wmi
    HAS_WMI = True
except Exception:
    wmi = None
    HAS_WMI = False

# 可選：指定 VapourSynth 腳本 (.vpy) 後，優先使用該管線抓取畫面亮度
# PowerShell 範例：$env:BRIGHTNESS_VS_SCRIPT='C:\path\to\source.vpy'
VAPOURSYNTH_SCRIPT_PATH = os.environ.get("BRIGHTNESS_VS_SCRIPT", "").strip()

# 先前支援透過 UDP 傳輸每幀亮度的機制已移除，改為透過 TCP（NetworkMonitorServer）以固定頻率廣播亮度。

SETTINGS_FILE = "settings.json"
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)

# 自動亮度公式係數（可在此統一調整）
# 當前亮度 = (avg * AUTO_BRIGHTNESS_CONTENT_COEFF + backlight * weight) / (AUTO_BRIGHTNESS_CONTENT_COEFF + weight)
AUTO_BRIGHTNESS_CONTENT_COEFF = 1.0
AUTO_BRIGHTNESS_CONTENT_COEFF_MIN_FACTOR = 0.5
AUTO_BRIGHTNESS_CONTENT_COEFF_MAX_FACTOR = 1.5
AUTO_BRIGHTNESS_WEIGHT_DEFAULT = 1.0

MODIFIER_ORDER = ["Alt", "Ctrl", "Shift", "Win"]
SHORTCUT_MODIFIER_OPTIONS = ["None"] + MODIFIER_ORDER
SHORTCUT_KEY_OPTIONS = (
    [f"NumPad{i}" for i in range(10)]
    + ["NumPad."]
    + [str(i) for i in range(10)]
    + [chr(code) for code in range(ord("A"), ord("Z") + 1)]
    + [f"F{i}" for i in range(1, 13)]
    + ["Left", "Up", "Right", "Down", "PageUp", "PageDown", "Home", "End"]
    + ["滑鼠左鍵", "滑鼠右鍵", "滑鼠中鍵", "滑鼠上一頁", "滑鼠下一頁"]
)
SHORTCUT_TYPE_OPTIONS = ["絕對值", "+Step", "-Step", "切換自動亮度"]
KEY_NAME_TO_VK = {
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
    "滑鼠左鍵": 0x01,   # VK_LBUTTON
    "滑鼠右鍵": 0x02,   # VK_RBUTTON
    "滑鼠中鍵": 0x04,   # VK_MBUTTON
    "滑鼠上一頁": 0x05, # VK_XBUTTON1
    "滑鼠下一頁": 0x06, # VK_XBUTTON2
}
DEFAULT_LEVEL_SHORTCUTS = [
    {"modifiers": ["Ctrl"], "key": "NumPad0", "type": "絕對值", "value": 0},
    {"modifiers": ["Ctrl"], "key": "NumPad1", "type": "絕對值", "value": 25},
    {"modifiers": ["Ctrl"], "key": "NumPad2", "type": "絕對值", "value": 50},
    {"modifiers": ["Ctrl"], "key": "NumPad3", "type": "絕對值", "value": 75},
    {"modifiers": ["Ctrl"], "key": "NumPad.", "type": "絕對值", "value": 100},
]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


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
    if not HAS_DXGI:
        return []

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


def get_monitor_display_name(monitor, index, caps=None):
    if isinstance(caps, dict):
        model = caps.get("model", "").strip()
        if model:
            return model

    for attr_name in ("name", "display_name", "description", "model", "monitor_name"):
        value = getattr(monitor, attr_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for attr_name in ("manufacturer", "brand"):
        value = getattr(monitor, attr_name, None)
        if isinstance(value, str) and value.strip():
            return f"{value.strip()} {index + 1}"

    return f"Display {index + 1}"


def _wmi_brightness_supported():
    if not HAS_WMI or wmi is None:
        return False
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


def qt_key_event_to_name(event):
    key = event.key()
    modifiers = event.modifiers()

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

    return None


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

        self.trigger_key1 = "Alt"
        self.trigger_key2 = "Win"
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

    def set_trigger_shortcut(self, key1, key2):
        self.trigger_key1 = key1
        self.trigger_key2 = key2

    def set_level_shortcuts(self, shortcuts):
        normalized_shortcuts = []
        for shortcut in shortcuts:
            key_name = shortcut.get("key")
            vk = KEY_NAME_TO_VK.get(key_name)
            if vk is None:
                continue

            modifiers = tuple(normalize_modifiers(shortcut.get("modifiers", [])))
            sc_type = shortcut.get("type", "絕對值")
            value = int(max(0, min(100, shortcut.get("value", 0))))
            normalized_shortcuts.append({
                "vk": vk,
                "modifiers": modifiers,
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

    def _get_pressed_modifiers(self):
        return tuple(modifier for modifier in MODIFIER_ORDER if self._is_modifier_pressed(modifier))

    def _match_level_shortcut(self, vk):
        pressed_modifiers = self._get_pressed_modifiers()
        for shortcut in self.level_shortcuts:
            if shortcut["vk"] == vk and shortcut["modifiers"] == pressed_modifiers:
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

                    key1_pressed = self._is_modifier_pressed(self.trigger_key1)
                    key2_pressed = self._is_modifier_pressed(self.trigger_key2)

                    if key1_pressed and key2_pressed:
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

        if monitor is None:
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
                    int(monitor.get_luminance())
                    self.brightness_supported = True
                except Exception:
                    pass
                try:
                    int(monitor.get_contrast())
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

    def set_available(self, available):
        self.available = available

    def read_current_levels(self):
        if not self.available or self.monitor is None:
            return None, None
        brightness = None
        contrast = None
        try:
            with self.lock:
                with self.monitor as m:
                    try:
                        brightness = int(m.get_luminance())
                    except Exception:
                        brightness = None
                    try:
                        contrast = int(m.get_contrast())
                    except Exception:
                        contrast = None
        except Exception:
            pass

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
        super().__init__(monitor_wrapper.name)

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
        b_min, b_max = self.monitor.brightness_range
        c_min, c_max = self.monitor.contrast_range
        brightness = self.pending_brightness if self.pending_brightness is not None else self.b_slider.slider.value()
        contrast = self.pending_contrast if self.pending_contrast is not None else self.c_slider.slider.value()

        if not self.monitor.contrast_supported:
            b_range = max(0, b_max - b_min)
            link_value = 0 if b_range <= 0 else int(round(((max(b_min, min(b_max, int(round(brightness))) - b_min) / b_range) * 100)))
        else:
            b_range = max(0, b_max - b_min)
            c_range = max(0, c_max - c_min)
            total = b_range + c_range
            if total <= 0:
                link_value = 0
            else:
                brightness = max(b_min, min(b_max, int(round(brightness))))
                contrast = max(c_min, min(c_max, int(round(contrast))))
                if brightness <= b_min:
                    units = max(0, min(c_range, contrast - c_min))
                else:
                    units = c_range + max(0, min(b_range, brightness - b_min))
                link_value = int(round((units / total) * 100))

        link_value = max(0, min(100, link_value))
        self.link_slider.slider.blockSignals(True)
        self.link_slider.slider.setValue(link_value)
        self.link_slider.value_label.setText(str(link_value))
        self.link_slider.slider.blockSignals(False)
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
        if not self.monitor.contrast_supported:
            b_min, b_max = self.monitor.brightness_range
            b_range = max(0, b_max - b_min)
            brightness = b_min + (float(percent) / 100.0) * b_range
            brightness = max(b_min, min(b_max, int(round(brightness))))
            self.pending_brightness = brightness
            self.pending_contrast = 0
            self.sync_sliders(brightness, 0)
            self.link_slider.slider.blockSignals(True)
            self.link_slider.slider.setValue(int(round(percent)))
            self.link_slider.value_label.setText(str(int(round(percent))))
            self.link_slider.slider.blockSignals(False)
            self.restart()
            self.value_changed.emit(percent)
            return
        b_min, b_max = self.monitor.brightness_range
        c_min, c_max = self.monitor.contrast_range

        b_range = b_max - b_min
        c_range = c_max - c_min
        total = b_range + c_range
        value = percent / 100 * total

        if value <= c_range:
            contrast = c_min + value
            brightness = b_min
        else:
            contrast = c_max
            brightness = b_min + (value - c_range)

        self.pending_brightness = brightness
        self.pending_contrast = contrast

        self.sync_sliders(brightness, contrast)
        self.link_slider.slider.blockSignals(True)
        self.link_slider.slider.setValue(int(round(percent)))
        self.link_slider.value_label.setText(str(int(round(percent))))
        self.link_slider.slider.blockSignals(False)
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
        super().__init__(f"{monitor_wrapper.name} Settings")
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
        shortcut = shortcut or {"modifiers": ["Ctrl"], "key": "NumPad0", "type": "絕對值", "value": 0}

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.mod1_combo = QtWidgets.QComboBox()
        self.mod1_combo.addItems(SHORTCUT_MODIFIER_OPTIONS)
        self.mod2_combo = QtWidgets.QComboBox()
        self.mod2_combo.addItems(SHORTCUT_MODIFIER_OPTIONS)
        self.key_button = KeyCaptureButton()
        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.addItems(SHORTCUT_TYPE_OPTIONS)
        self.value_label = QtWidgets.QLabel("亮度 %")
        self.value_spin = QtWidgets.QSpinBox()
        self.value_spin.setRange(0, 100)
        self.value_label.setFixedWidth(self.value_label.sizeHint().width())
        self.value_spin.setFixedWidth(max(70, self.value_spin.sizeHint().width()))
        self.remove_button = QtWidgets.QPushButton("刪除")

        layout.addWidget(QtWidgets.QLabel("快捷鍵"))
        layout.addWidget(self.mod1_combo)
        layout.addWidget(QtWidgets.QLabel("+"))
        layout.addWidget(self.mod2_combo)
        layout.addWidget(QtWidgets.QLabel("+"))
        layout.addWidget(self.key_button)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.value_label)
        layout.addWidget(self.value_spin)
        layout.addWidget(self.remove_button)
        self.setLayout(layout)

        self.mod1_combo.currentTextChanged.connect(self.changed)
        self.mod2_combo.currentTextChanged.connect(self.changed)
        self.key_button.key_changed.connect(self.changed)
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
        modifiers = normalize_modifiers(shortcut.get("modifiers", []))
        key = shortcut.get("key", "NumPad0")
        sc_type = shortcut.get("type", "絕對值")
        value = int(max(0, min(100, shortcut.get("value", 0))))

        mod1 = modifiers[0] if len(modifiers) >= 1 else "None"
        mod2 = modifiers[1] if len(modifiers) >= 2 else "None"

        self.mod1_combo.setCurrentText(mod1 if mod1 in SHORTCUT_MODIFIER_OPTIONS else "None")
        self.mod2_combo.setCurrentText(mod2 if mod2 in SHORTCUT_MODIFIER_OPTIONS else "None")
        self.key_button.set_key_name(key if key in KEY_NAME_TO_VK else "NumPad0")
        self.type_combo.setCurrentText(sc_type if sc_type in SHORTCUT_TYPE_OPTIONS else "絕對值")
        self.value_spin.setValue(value)
        self._on_type_changed(self.type_combo.currentText())

    def get_data(self):
        modifiers = normalize_modifiers([self.mod1_combo.currentText(), self.mod2_combo.currentText()])
        sc_type = self.type_combo.currentText()
        return {
            "modifiers": modifiers,
            "key": self.key_button.key_name,
            "type": sc_type,
            "value": int(self.value_spin.value()) if sc_type == "絕對值" else 0,
        }


class KeyCaptureButton(QtWidgets.QPushButton):
    key_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.key_name = key_name if key_name in KEY_NAME_TO_VK else "NumPad0"
        self._capture_mode = False
        self.update_text()
        self.key_changed.emit(self.key_name)

    def update_text(self):
        self.setText(self.key_name)

    def keyPressEvent(self, event):
        if not self._capture_mode:
            return super().keyPressEvent(event)

        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._capture_mode = False
            self.update_text()
            event.accept()
            return

        key_name = qt_key_event_to_name(event)
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
            self._capture_mode = False
            self.update_text()
        super().focusOutEvent(event)


# =========================
# Screen Auto Brightness Analyzer
# =========================
# UDP luminance server 已移除；本程式改以 TCP 廣播亮度樣本 (NetworkMonitorServer.broadcast_luminance)


class _VapourSynthCapture:
    """VapourSynth 逐幀亮度擷取（原型）。"""

    def __init__(self, script_path):
        if not HAS_VAPOURSYNTH:
            raise RuntimeError("VapourSynth 不可用")
        if not script_path:
            raise RuntimeError("未設定 VapourSynth 腳本路徑")
        if not os.path.isfile(script_path):
            raise FileNotFoundError(f"找不到 VapourSynth 腳本: {script_path}")

        self.script_path = script_path
        self._frame_index = 0
        self._frame_lock = threading.Lock()

        core = vs.core
        context = {"vs": vs, "core": core}
        with open(script_path, "r", encoding="utf-8") as f:
            code = compile(f.read(), script_path, "exec")
        exec(code, context, context)

        clip = None
        get_outputs = getattr(vs, "get_outputs", None)
        if callable(get_outputs):
            outputs = get_outputs()
            if outputs:
                first_output = next(iter(outputs.values()))
                clip = getattr(first_output, "clip", first_output)

        if clip is None:
            clip = context.get("clip") or context.get("video")

        if clip is None:
            raise RuntimeError(".vpy 需透過 set_output() 輸出 clip，或定義 `clip` / `video`")
        if getattr(clip, "num_frames", 0) <= 0:
            raise RuntimeError("VapourSynth clip 沒有可讀取影格")

        if clip.format is None:
            raise RuntimeError("VapourSynth clip 格式未知")

        if clip.format.color_family == vs.GRAY:
            luma_clip = clip
        elif clip.format.color_family == vs.YUV:
            luma_clip = core.std.ShufflePlanes(clip, 0, vs.GRAY)
        else:
            # RGB / 其他格式先轉灰階
            luma_clip = core.resize.Bicubic(clip, format=vs.GRAY8, matrix_s="709")

        self._stats_clip = core.std.PlaneStats(luma_clip)
        self._num_frames = int(self._stats_clip.num_frames)

    def capture_luminance(self):
        with self._frame_lock:
            frame_index = max(0, min(self._frame_index, self._num_frames - 1))
            frame = self._stats_clip.get_frame(frame_index)
            avg = frame.props.get("PlaneStatsAverage")
            self._frame_index += 1
            if self._frame_index >= self._num_frames:
                self._frame_index = 0

        if avg is None:
            return None
        return max(0.0, min(100.0, float(avg) * 100.0))


class _CaptureThread(QtCore.QThread):
    result_ready = QtCore.pyqtSignal(float, str)  # (亮度 0-100, 來源: "VPY"/"VS"/"DXGI")

    _dxgi_cameras = {}
    _dxgi_lock = threading.Lock()
    _dxgi_disabled = False
    _vs_capture = None
    _vs_lock = threading.Lock()
    _vs_disabled = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.use_dxgi = HAS_DXGI and HAS_NUMPY
        self.use_vapoursynth = HAS_VAPOURSYNTH and bool(VAPOURSYNTH_SCRIPT_PATH)
        # UDP 已移除；改由 TCP server 在捕捉到亮度時廣播。

    @classmethod
    def _get_dxgi_camera(cls, device_idx=0, output_idx=0):
        if not HAS_DXGI or cls._dxgi_disabled:
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
                cls._dxgi_disabled = True
                cls._dxgi_cameras = {}
            else:
                cls._dxgi_cameras.pop((int(device_idx), int(output_idx)), None)

    @classmethod
    def _get_vapoursynth_capture(cls):
        if not HAS_VAPOURSYNTH or cls._vs_disabled or not VAPOURSYNTH_SCRIPT_PATH:
            return None
        with cls._vs_lock:
            if cls._vs_capture is None:
                cls._vs_capture = _VapourSynthCapture(VAPOURSYNTH_SCRIPT_PATH)
            return cls._vs_capture

    @classmethod
    def _disable_vapoursynth(cls):
        with cls._vs_lock:
            cls._vs_disabled = True
            cls._vs_capture = None

    def _capture_vapoursynth(self):
        """使用 VapourSynth 方式逐幀擷取（原型）"""
        try:
            capture = self._get_vapoursynth_capture()
            if capture is None:
                return None
            return capture.capture_luminance()
        except Exception as e:
            print(f"VapourSynth 擷取錯誤: {e}")
            self._disable_vapoursynth()
            self.use_vapoursynth = False
            return None

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
            self._disable_dxgi(device_idx, output_idx)
            self.use_dxgi = False
            return None

    def run(self):
        result = None
        source = "—"

        # 優先使用 UDP 區間平均（涵蓋自上次截圖以來的所有幀）
        # 即使目前在 DXGI 模式，只要 VPY 重新開始發送資料就會自動切回 VPY
        # 取消 UDP 路徑：改由本程式依序呼叫 VS/DXGI 擷取亮度

        # UDP 沒有資料時，嘗試走 VapourSynth 腳本管線
        if result is None and self.use_vapoursynth:
            result = self._capture_vapoursynth()
            if result is not None:
                source = "VS"

        # VS 不可用時回退 DXGI
        if result is None:
            result = self._capture_dxgi()
            if result is not None:
                source = "DXGI"

        if result is not None:
            self.result_ready.emit(result, source)


class ScreenAnalyzer(QtCore.QObject):
    adjust_requested = QtCore.pyqtSignal(float)  # 每 tick 建議調整的百分比（可正可負）
    luminance_updated = QtCore.pyqtSignal(float)  # 即時畫面亮度 0-100
    luminance_source_updated = QtCore.pyqtSignal(str)  # 亮度來源："VPY" / "VS" / "DXGI" / "—"

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
        self.threshold = 5      # 反應門檻
        self.weight = AUTO_BRIGHTNESS_WEIGHT_DEFAULT  # 背光權重
        self.adjust_step_percent = 0.5  # 每個調整週期允許改變的百分比
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

        # 截圖 timer：依設定間隔判斷方向
        self._capture_timer = QtCore.QTimer(self)
        self._capture_timer.setInterval(int(self.capture_interval_seconds * 1000))
        self._capture_timer.timeout.connect(self._tick_capture)

        # 微調 timer：每次調整最細 1 経 DDC level
        self._adjust_timer = QtCore.QTimer(self)
        self._adjust_timer.setInterval(100)  # 固定每 100ms 調整一級
        self._adjust_timer.timeout.connect(self._tick_adjust)

    def start(self):
        self._capture_timer.start()

    def stop(self):
        self._capture_timer.stop()
        self._adjust_timer.stop()

    def set_current_ddc(self, value):
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

    def set_adjust_step_percent(self, percent):
        percent = max(0.01, min(100.0, float(percent)))
        self.adjust_step_percent = percent

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

        if abs(diff) <= self.threshold:
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
            if abs(self.target - effective) <= self.threshold:
                self._direction = 0
                self._adjust_timer.stop()
                return

        remaining = self._desired_ddc - self._current_ddc_float
        if abs(remaining) <= 1e-6:
            self._direction = 0
            self._adjust_timer.stop()
            return

        # 每 tick 只提出一個調整量；實際背光狀態由 MainWindow 套用後回寫。
        step = min(abs(remaining), self.adjust_step_percent)
        delta_percent = step if remaining > 0 else -step
        next_ddc_float = max(0.0, min(100.0, self._current_ddc_float + delta_percent))
        delta_percent = next_ddc_float - self._current_ddc_float

        if abs(delta_percent) <= 1e-9:
            self._direction = 0
            self._adjust_timer.stop()
            return

        self.adjust_requested.emit(delta_percent)

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
        if Zeroconf is None or ServiceInfo is None:
            print("NetServer unavailable: zeroconf is not installed")
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
                        response = self._process_request(line)
                        should_broadcast = bool(response.pop("_broadcast_monitors", False))
                        try:
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
            ok = bool(self._set_app_state_callback(req.get("global_link"), req.get("auto_target")))
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
        if Zeroconf is None or ServiceBrowser is None:
            print("Network client unavailable: zeroconf is not installed")
            return
        self._running = True
        self._zeroconf = Zeroconf()
        listener = _ServiceListener(self._on_service_changed)
        self._browser = ServiceBrowser(self._zeroconf, NETWORK_SERVICE_TYPE, listener)
        # 定期刷新
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
            self._query_server(name)

    def _emit_remote_monitors(self):
        all_monitors = []
        for srv_name, entry in self._discovered_servers.items():
            hostname = entry["info"].properties.get(b"hostname", srv_name).decode()
            for mon in entry.get("monitors", []):
                mon["_remote_server"] = hostname
                mon["_remote_name"] = srv_name
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
        entry["monitors"] = monitors if isinstance(monitors, list) else []
        state = {}
        if "global_link" in message:
            state["global_link"] = message.get("global_link")
        if "auto_target" in message:
            state["auto_target"] = message.get("auto_target")
        if state:
            entry["state"] = state
        info = entry["info"]
        hostname = info.properties.get(b"hostname", b"unknown").decode()
        print(f"Remote server {hostname}: {len(entry['monitors'])} monitors")
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
                        if ok:
                            self._handle_server_message(server_name, resp)
                        return ok
        except Exception as e:
            print(f"Remote set error: {e}")
        return False

    def remote_set_state(self, server_name, global_link=None, auto_target=None):
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
                if global_link is not None:
                    req["global_link"] = int(global_link)
                if auto_target is not None:
                    req["auto_target"] = int(auto_target)
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
                        if ok:
                            self._handle_server_message(server_name, resp)
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


class RemoteMonitorWrapper:
    """遠端螢幕的 MonitorWrapper 等價物件（唯讀/可遠端設定）。"""
    def __init__(self, data, server_name):
        self.name = data.get("name", f"Remote {server_name}")
        self._server_name = server_name
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
    remote_state_applied = QtCore.pyqtSignal(object, object)
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
        self._pending_network_global_link = None
        self._pending_network_auto_target = None
        self.global_link_value = 0
        self.step_value = 5.0
        self.shortcut_key1 = "Alt"
        self.shortcut_key2 = "Win"
        self.auto_start_enabled = False
        self.level_shortcuts = [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
        self.global_hook = None
        self._loading_settings = False

        # 網路功能
        self._network_server_enabled = False
        self._network_client_enabled = False
        self._remote_wrappers = []
        self._remote_widgets = []
        self._remote_monitor_data = []
        self.remote_servers_map = {}
        self._pending_remote_sets = {}

        # 畫面自動調整
        self.auto_adjust_enabled = False
        self.auto_adjust_target = 50
        self.auto_adjust_threshold = 5
        self.auto_adjust_weight = AUTO_BRIGHTNESS_WEIGHT_DEFAULT
        self.auto_adjust_capture_interval = 1.0
        self.auto_adjust_step_percent = 0.5
        self.auto_adjust_resource_saving_enabled = True
        self.auto_adjust_resource_saving_idle_seconds = 5.0
        self.screen_analyzers = []
        self.screen_analyzer = None
        self._monitor_auto_states = []
        self.current_effective_brightness = None

        # 網路功能
        self._network_server_enabled = False
        self._network_client_enabled = False
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

        wrappers = [MonitorWrapper(m, i) for i, m in enumerate(get_monitors())]
        self.monitor_wrappers = []
        for w in wrappers:
            if not w.supported:
                w.available = False
            self.monitor_wrappers.append(w)

        self._init_screen_analyzers()
        self._known_monitor_names = sorted(w.name for w in self.monitor_wrappers)
        self._prev_raw_monitor_count = len(list(get_monitors()))

        # 無支援螢幕時，從設定檔恢復已知螢幕（保持 UI 顯示但灰階）
        if not self.monitor_wrappers:
            self._restore_known_monitors_from_settings()

        self.monitor_widgets = []
        self.monitor_range_widgets = []
        self.shortcut_rows = []
        self._update_analyzer_levels()

        # 防抖存檔 Timer (避免頻繁寫入硬碟)
        self.save_timer = QtCore.QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_settings)

        # 定時偵測螢幕熱插拔（每 10 秒輕量檢查一次）
        self._monitor_watch_timer = QtCore.QTimer()
        self._monitor_watch_timer.timeout.connect(self._check_monitors_changed)
        self._monitor_watch_timer.start(10000)

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
        QtCore.QTimer.singleShot(3000, self._retry_initial_monitor_detection_if_empty)

    def _has_available_local_monitor(self):
        return any(
            getattr(wrapper, "available", False) and not isinstance(wrapper, RemoteMonitorWrapper)
            for wrapper in self.monitor_wrappers
        )

    def _retry_initial_monitor_detection_if_empty(self):
        if self._is_quitting or self._has_available_local_monitor():
            return
        print("No local DDC/WMI monitor detected at startup; retrying monitor detection")
        self.refresh_monitors()

    def _restore_known_monitors_from_settings(self):
        """從設定檔恢復已知螢幕名稱，建立不可用的 MonitorWrapper 佔位"""
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            known_names = saved.get("known_monitor_names", [])
            monitors_data = saved.get("monitors", [])
            for i, name in enumerate(known_names):
                b_range = [0, 100]
                c_range = [0, 100]
                if i < len(monitors_data):
                    b_range = list(monitors_data[i].get("b_range", [0, 100]))
                    c_range = list(monitors_data[i].get("c_range", [0, 100]))
                w = MonitorWrapper(monitor=None, index=i, name=name,
                                   b_range=b_range, c_range=c_range)
                w.available = False
                w.supported = False
                self.monitor_wrappers.append(w)
            self._known_monitor_names = known_names
        except Exception:
            pass

    def _update_monitor_availability(self):
        """重新偵測螢幕，更新現有 wrappers 的 available 狀態，必要時新增/移除"""
        try:
            monitors = list(get_monitors())
        except Exception as e:
            print("Monitor detection error:", e)
            return

        # 建立名稱 → monitor 對照。重新偵測時必須更新既有 wrapper 的 monitor 物件，
        # 不能只切 available，否則啟動時漏抓到的螢幕按「重新偵測」後仍是空殼。
        detected_wrappers = {}
        for i, m in enumerate(monitors):
            try:
                wrapper = MonitorWrapper(m, i)
                if wrapper.supported:
                    detected_wrappers[wrapper.name] = wrapper
            except Exception:
                continue

        local_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]
        remote_wrappers = [w for w in self.monitor_wrappers if isinstance(w, RemoteMonitorWrapper)]
        current_names = {w.name for w in local_wrappers}

        # 新增：完全新的螢幕
        for name, detected in detected_wrappers.items():
            if name not in current_names:
                w = detected
                w.index = len(local_wrappers)
                w.available = True
                local_wrappers.append(w)
                print(f"Monitor added: {name}")

        # 更新：現有螢幕的可用/不可用狀態
        for idx, w in enumerate(local_wrappers):
            detected = detected_wrappers.get(w.name)
            if detected is not None:
                if not w.available:
                    print(f"Monitor became available: {w.name}")
                preserved_b_range = list(w.brightness_range)
                preserved_c_range = list(w.contrast_range)
                w.monitor = detected.monitor
                w.lock = detected.lock
                w.index = idx
                w.supported = detected.supported
                w.brightness_supported = detected.brightness_supported
                w.contrast_supported = detected.contrast_supported
                w.wmi_supported = detected.wmi_supported
                if preserved_b_range:
                    w.brightness_range = preserved_b_range
                if preserved_c_range:
                    w.contrast_range = preserved_c_range
                w.available = True
            else:
                if w.available:
                    print(f"Monitor became unavailable: {w.name}")
                w.available = False
                w.monitor = None

        self.monitor_wrappers = local_wrappers + remote_wrappers
        self._known_monitor_names = sorted(w.name for w in local_wrappers)
        self._prev_raw_monitor_count = len(monitors)

        self._sync_local_monitor_widgets()

        self.sync_ui_with_current_monitor_levels()
        self.refresh_tray_display()
        self._init_screen_analyzers()

    def _sync_local_monitor_widgets(self):
        local_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]
        local_names = {w.name for w in local_wrappers}

        for widget in list(self.monitor_widgets):
            if widget.monitor.name not in local_names:
                self.monitor_widgets.remove(widget)
                widget.deleteLater()

        for wrapper in local_wrappers:
            widget = next((w for w in self.monitor_widgets if w.monitor.name == wrapper.name), None)
            if widget is None:
                widget = MonitorWidget(wrapper, self.threadpool)
                widget.value_changed.connect(self.on_monitor_link_changed)
                self.monitor_widgets.append(widget)
                self._insert_monitor_widget(widget)
            else:
                widget.monitor = wrapper
            widget.set_available(wrapper.available)
            widget.set_ranges(wrapper.brightness_range, wrapper.contrast_range)

    def _insert_monitor_widget(self, widget):
        if not hasattr(self, "main_page") or not self.main_page:
            return
        layout = self.main_page.layout()
        if layout is None:
            return
        insert_idx = layout.count()
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.spacerItem() is not None:
                insert_idx = i
                break
        layout.insertWidget(insert_idx, widget)

    def _configure_screen_analyzer(self, analyzer):
        analyzer.enabled = self.auto_adjust_enabled
        analyzer.target = self.auto_adjust_target
        analyzer.threshold = self.auto_adjust_threshold
        analyzer.weight = self.auto_adjust_weight
        analyzer.set_capture_interval_seconds(self.auto_adjust_capture_interval)
        analyzer.set_adjust_step_percent(self.auto_adjust_step_percent)
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

    def _check_monitors_changed(self):
        """輕量檢查螢幕數量是否變化，變化時更新可用狀態"""
        if self._loading_settings:
            return
        try:
            current_count = len(list(get_monitors()))
        except Exception:
            return
        if current_count != self._prev_raw_monitor_count:
            print(f"Monitor raw count changed: {self._prev_raw_monitor_count} → {current_count}")
            self._update_monitor_availability()

    def _rebuild_monitor_ui(self):
        """螢幕熱插拔時安全重建UI（保留 wrapper，只更新可用性）"""
        self._update_monitor_availability()

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
            layout.addWidget(QtWidgets.QLabel("未偵測到可控制的螢幕"))

        for wrapper in self.monitor_wrappers:
            monitor_widget = MonitorWidget(wrapper, self.threadpool)
            monitor_widget.value_changed.connect(self.on_monitor_link_changed)
            monitor_widget.set_available(wrapper.available)
            self.monitor_widgets.append(monitor_widget)
            layout.addWidget(monitor_widget)

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

        global_grid.addWidget(QtWidgets.QLabel("Step"), 0, 0)
        global_grid.addWidget(self.step_combo, 0, 1)
        global_grid.addWidget(self.autostart_checkbox, 1, 0, 1, 2)
        global_group.setLayout(global_grid)
        gen_layout.addWidget(global_group)

        # 滾輪快捷鍵 + 鍵盤快捷鍵放 General
        wheel_group = QtWidgets.QGroupBox("滾輪快捷鍵")
        wheel_grid = QtWidgets.QGridLayout()
        wheel_grid.setContentsMargins(6, 6, 6, 6)
        wheel_grid.setSpacing(6)

        self.shortcut_key1_combo = QtWidgets.QComboBox()
        self.shortcut_key1_combo.addItems(self.HOTKEY_OPTIONS)
        self.shortcut_key1_combo.setCurrentText(self.shortcut_key1)
        self.shortcut_key1_combo.currentTextChanged.connect(self.on_shortcut_changed)

        self.shortcut_key2_combo = QtWidgets.QComboBox()
        self.shortcut_key2_combo.addItems(self.HOTKEY_OPTIONAL_OPTIONS)
        self.shortcut_key2_combo.setCurrentText(self.shortcut_key2)
        self.shortcut_key2_combo.currentTextChanged.connect(self.on_shortcut_changed)

        wheel_grid.addWidget(QtWidgets.QLabel("觸發鍵"), 0, 0)
        wheel_grid.addWidget(self.shortcut_key1_combo, 0, 1)
        wheel_grid.addWidget(QtWidgets.QLabel("+"), 0, 2)
        wheel_grid.addWidget(self.shortcut_key2_combo, 0, 3)
        wheel_group.setLayout(wheel_grid)
        gen_layout.addWidget(wheel_group)

        shortcut_group = QtWidgets.QGroupBox("鍵盤快捷鍵")
        shortcut_layout = QtWidgets.QVBoxLayout()
        shortcut_layout.setContentsMargins(6, 6, 6, 6)
        shortcut_layout.setSpacing(6)

        shortcut_hint = QtWidgets.QLabel("可任意新增或刪除快捷鍵。先選修飾鍵，再點主鍵按鈕後直接按下鍵盤主鍵。")
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
        if not (HAS_DXGI and HAS_NUMPY):
            self.auto_adjust_checkbox.setEnabled(False)
            msg = "需要安裝依賴: "
            if not HAS_NUMPY:
                msg += "numpy (pip install numpy)"
            if not HAS_DXGI:
                msg += " dxcam (pip install dxcam)"
            self.auto_adjust_checkbox.setToolTip(msg)
        self.auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
        self.auto_adjust_checkbox.toggled.connect(self.on_auto_adjust_toggled)

        self.auto_adjust_threshold_spin = QtWidgets.QSpinBox()
        self.auto_adjust_threshold_spin.setRange(1, 50)
        self.auto_adjust_threshold_spin.setValue(self.auto_adjust_threshold)
        self.auto_adjust_threshold_spin.setSuffix(" %")
        self.auto_adjust_threshold_spin.setToolTip("畫面亮度與目標差距超過此值才觸發調整")
        self.auto_adjust_threshold_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

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

        self.auto_adjust_step_percent_spin = QtWidgets.QDoubleSpinBox()
        self.auto_adjust_step_percent_spin.setRange(0.01, 100.0)
        self.auto_adjust_step_percent_spin.setSingleStep(0.1)
        self.auto_adjust_step_percent_spin.setDecimals(2)
        self.auto_adjust_step_percent_spin.setValue(self.auto_adjust_step_percent)
        self.auto_adjust_step_percent_spin.setSuffix(" %")
        self.auto_adjust_step_percent_spin.setToolTip("每個調整時間間隔最多可變更的亮度百分比")
        self.auto_adjust_step_percent_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

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

        auto_grid.addWidget(self.auto_adjust_checkbox, 0, 0, 1, 4)
        auto_grid.addWidget(QtWidgets.QLabel("截圖間隔"), 1, 0)
        auto_grid.addWidget(self.auto_adjust_capture_interval_spin, 1, 1)
        auto_grid.addWidget(QtWidgets.QLabel("反應門檻"), 1, 2)
        auto_grid.addWidget(self.auto_adjust_threshold_spin, 1, 3)
        auto_grid.addWidget(QtWidgets.QLabel("調整級距"), 2, 0)
        auto_grid.addWidget(self.auto_adjust_step_percent_spin, 2, 1)
        auto_grid.addWidget(QtWidgets.QLabel("背光權重"), 2, 2)
        auto_grid.addWidget(self.auto_adjust_weight_spin, 2, 3)
        auto_grid.addWidget(self.auto_adjust_resource_saving_checkbox, 3, 0, 1, 2)
        auto_grid.addWidget(QtWidgets.QLabel("靜止門檻"), 3, 2)
        auto_grid.addWidget(self.auto_adjust_resource_saving_idle_spin, 3, 3)
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

        self.net_server_checkbox = QtWidgets.QCheckBox("啟用伺服器 (分享本機螢幕給區域網路)")
        self.net_server_checkbox.setChecked(self._network_server_enabled)
        self.net_server_checkbox.toggled.connect(self._on_net_server_toggled)

        self.net_client_checkbox = QtWidgets.QCheckBox("啟用用戶端 (發現並控制區域網路其他螢幕)")
        self.net_client_checkbox.setChecked(self._network_client_enabled)
        self.net_client_checkbox.toggled.connect(self._on_net_client_toggled)

        self.net_servers_label = QtWidgets.QLabel("已發現伺服器: 0")
        self.net_servers_label.setWordWrap(True)

        net_grid.addWidget(self.net_server_checkbox, 0, 0, 1, 2)
        net_grid.addWidget(self.net_client_checkbox, 1, 0, 1, 2)
        net_grid.addWidget(self.net_servers_label, 2, 0, 1, 2)
        # 已移除 UDP 防火牆按鈕（改為透過 TCP 廣播亮度）
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
        """重新偵測螢幕按鈕：更新可用狀態，保留所有已知螢幕"""
        self._update_monitor_availability()
        self._update_analyzer_levels()
        self.trigger_save()

    # ---- 網路功能 ----
    def _on_net_server_toggled(self, enabled):
        self._network_server_enabled = enabled
        if enabled:
            self._net_server.start()
        else:
            self._net_server.stop()
        self.trigger_save()

    def _on_net_client_toggled(self, enabled):
        self._network_client_enabled = enabled
        if enabled:
            self._net_client.start()
        else:
            self._net_client.stop()
            self._clear_remote_wrappers()
        self.trigger_save()

    # UDP 防火牆按鈕與 UAC 提升相關功能已移除（改用 TCP 廣播）。

    def _on_remote_monitors_updated(self, monitors):
        if hasattr(self, "net_servers_label"):
            server_count = len(set(m.get("_remote_server", "?") for m in monitors))
            self.net_servers_label.setText(f"已發現伺服器: {server_count} 台，共 {len(monitors)} 個螢幕")

        self.monitor_wrappers = [w for w in self.monitor_wrappers if not isinstance(w, RemoteMonitorWrapper)]

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
                wrapper = RemoteMonitorWrapper(data, srv)
                self._remote_wrappers.append(wrapper)
                widget = MonitorWidget(wrapper, self.threadpool)
                self._sync_remote_widget(widget, wrapper)
                widget.value_changed.connect(self._on_remote_monitor_link_changed)
                self._remote_widgets.append(widget)
                self.remote_servers_map[key] = widget

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

        self._rebuild_remote_widgets()
        self.refresh_tray_display()

    def _sync_remote_widget(self, widget, wrapper):
        brightness, contrast = wrapper.read_current_levels()
        if brightness is None:
            brightness = widget.b_slider.slider.value()
        if contrast is None:
            contrast = 0 if not wrapper.contrast_supported else widget.c_slider.slider.value()
        widget.sync_sliders(brightness, contrast)
        link_value = self._link_value_from_levels(wrapper, brightness, contrast)
        widget.link_slider.slider.blockSignals(True)
        widget.link_slider.slider.setValue(link_value)
        widget.link_slider.slider.blockSignals(False)
        widget.link_slider.value_label.setText(str(link_value))

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
            "global_link": int(round(self._pending_network_global_link if self._pending_network_global_link is not None else self.global_link_value)),
            "auto_target": int(round(self._pending_network_auto_target if self._pending_network_auto_target is not None else self.auto_adjust_target)),
        }

    def _remote_set_app_state(self, global_link, auto_target):
        if global_link is None and auto_target is None:
            return False
        if global_link is not None:
            self._pending_network_global_link = int(global_link)
        if auto_target is not None:
            self._pending_network_auto_target = int(auto_target)
        self.remote_state_applied.emit(global_link, auto_target)
        return True

    def _on_remote_state_updated(self, state):
        if not isinstance(state, dict) or self._applying_remote_network_state:
            return
        global_link = state.get("global_link")
        auto_target = state.get("auto_target")
        if global_link is None and auto_target is None:
            return
        if (
            (global_link is None or int(global_link) == int(round(self.global_link_value)))
            and (auto_target is None or int(auto_target) == int(round(self.auto_adjust_target)))
        ):
            return
        self._on_remote_state_applied(global_link, auto_target)

    def _on_remote_state_applied(self, global_link, auto_target):
        if self._applying_remote_network_state:
            return
        self._applying_remote_network_state = True
        try:
            if auto_target is not None and int(auto_target) != int(round(self.auto_adjust_target)):
                self.set_auto_adjust_target(int(auto_target), trigger_save=False)
            if global_link is not None and int(global_link) != int(round(self.global_link_value)):
                self.update_global_link(int(global_link))
            if auto_target is not None:
                self._pending_network_auto_target = None
            if global_link is not None:
                self._pending_network_global_link = None
            self.trigger_save()
        finally:
            self._applying_remote_network_state = False

    def _sync_app_state_to_remote_servers(self, global_link=None, auto_target=None):
        if self._applying_remote_network_state or not self._network_client_enabled:
            return
        for server_name in list(getattr(self._net_client, "_discovered_servers", {}).keys()):
            self._net_client.remote_set_state(server_name, global_link=global_link, auto_target=auto_target)

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
            link_value = self._link_value_from_levels(wrapper, b, c)
            widget.link_slider.slider.blockSignals(True)
            widget.link_slider.slider.setValue(link_value)
            widget.link_slider.slider.blockSignals(False)
            widget.link_slider.value_label.setText(str(link_value))
            widget.restart()
            break
        self._sync_global_link_from_available_monitors()
        self._update_auto_adjust_info()
        self.refresh_tray_display()
        self.trigger_save()
        self._broadcast_monitor_state_if_server_enabled()

    def _clear_remote_wrappers(self):
        for w in self._remote_wrappers:
            if w in self.monitor_wrappers:
                self.monitor_wrappers.remove(w)
        for widget in self._remote_widgets:
            widget.deleteLater()
        self._remote_wrappers.clear()
        self._remote_widgets.clear()
        self.remote_servers_map.clear()
        self._rebuild_remote_widgets()

    def _rebuild_remote_widgets(self):
        """將遠端螢幕加入主頁面 layout（在 local 螢幕之後）"""
        if not hasattr(self, "main_page") or not self.main_page:
            return
        layout = self.main_page.layout()
        if layout is None:
            return
        insert_idx = max(0, layout.count() - 1)
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.spacerItem() is not None:
                insert_idx = i
                break
        # 移除所有現有遠端 widget（避免重複）
        for w in self._remote_widgets:
            if w.isVisible():
                layout.removeWidget(w)
        # 重新插入
        for w in self._remote_widgets:
            layout.insertWidget(insert_idx, w)
            insert_idx += 1

    def _on_remote_monitor_link_changed(self, percent):
        """遠端螢幕聯動滑桿變更 → 透過網路發送 set 指令"""
        widget = self.sender()
        if not isinstance(widget, MonitorWidget):
            return
        wrapper = widget.monitor
        if not isinstance(wrapper, RemoteMonitorWrapper):
            return
        srv = wrapper._server_name
        b_min, b_max = wrapper.brightness_range
        c_min, c_max = wrapper.contrast_range
        if not wrapper.contrast_supported:
            b_range = max(0, b_max - b_min)
            brightness = b_min if b_range <= 0 else b_min + (float(percent) / 100.0) * b_range
            brightness = int(round(max(b_min, min(b_max, brightness))))
            self._queue_remote_set(srv, wrapper.name, brightness, None, wrapper)
            return
        b_range = b_max - b_min
        c_range = c_max - c_min
        total = b_range + c_range
        if total <= 0:
            return
        value = percent / 100 * total
        if value <= c_range:
            contrast = c_min + value
            brightness = b_min
        else:
            contrast = c_max
            brightness = b_min + (value - c_range)
        brightness = int(round(brightness))
        contrast = int(round(contrast))
        self._queue_remote_set(srv, wrapper.name, brightness, contrast, wrapper)

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

    def _link_value_from_levels(self, wrapper, brightness, contrast):
        b_min, b_max = wrapper.brightness_range
        c_min, c_max = wrapper.contrast_range

        if not getattr(wrapper, "contrast_supported", True):
            if brightness is None:
                brightness = b_min
            brightness = max(b_min, min(b_max, int(brightness)))
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

        brightness = max(b_min, min(b_max, int(brightness)))
        contrast = max(c_min, min(c_max, int(contrast)))

        # Link 滑桿的對應：先走對比，再走亮度
        if brightness <= b_min:
            units = max(0, min(c_range, contrast - c_min))
        else:
            units = c_range + max(0, min(b_range, brightness - b_min))

        return int(round((units / total) * 100))

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

                link_value = self._link_value_from_levels(wrapper, brightness, contrast)
                widget.link_slider.slider.blockSignals(True)
                widget.link_slider.slider.setValue(link_value)
                widget.link_slider.slider.blockSignals(False)
                widget.link_slider.value_label.setText(str(link_value))
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
        self.update_auto_adjust_controls_visibility()
        self.refresh_tray_display()
        self.trigger_save()

    def on_auto_adjust_settings_changed(self):
        self.auto_adjust_threshold = int(self.auto_adjust_threshold_spin.value())
        self.auto_adjust_weight = float(self.auto_adjust_weight_spin.value())
        self.auto_adjust_capture_interval = float(self.auto_adjust_capture_interval_spin.value())
        self.auto_adjust_step_percent = float(self.auto_adjust_step_percent_spin.value())
        self.auto_adjust_resource_saving_enabled = bool(self.auto_adjust_resource_saving_checkbox.isChecked())
        self.auto_adjust_resource_saving_idle_seconds = float(self.auto_adjust_resource_saving_idle_spin.value())
        self._for_each_screen_analyzer(self._configure_screen_analyzer)
        self._sync_screen_analyzer_enabled()
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(self.auto_adjust_target)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(self.auto_adjust_target))
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

    def _available_link_values(self):
        values = []
        for idx, (wrapper, widget) in enumerate(zip(self.monitor_wrappers, self.monitor_widgets)):
            if not getattr(wrapper, "available", False):
                continue
            try:
                values.append((idx, int(widget.link_slider.slider.value())))
            except RuntimeError:
                pass
        return values

    def _sync_global_link_from_available_monitors(self):
        values = self._available_link_values()
        if not values:
            return
        self.global_link_value = int(round(sum(value for _idx, value in values) / len(values)))
        for idx, value in values:
            if idx < len(self.screen_analyzers) and self.screen_analyzers[idx] is not None:
                self.screen_analyzers[idx].set_current_ddc(value)
        self._sync_main_global_link_controls()

    def set_auto_adjust_target(self, value, trigger_save=True):
        value = max(0, min(100, self.snap_to_step(value)))
        value = int(round(value))
        self.auto_adjust_target = value
        self._for_each_screen_analyzer(lambda analyzer: setattr(analyzer, "target", value))
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(value)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(value))
        self._update_auto_adjust_info()
        if trigger_save:
            self.trigger_save()
        self._sync_app_state_to_remote_servers(auto_target=value)

    def adjust_auto_adjust_target(self, delta):
        new_val = self.snap_to_step(self.auto_adjust_target + delta)
        self.set_auto_adjust_target(new_val)

    def update_auto_adjust_controls_visibility(self):
        pass  # auto_target_group 永遠顯示

    def _update_analyzer_levels(self):
        """ 計算所有螢幕亮度+對比總級數並通知 ScreenAnalyzer """
        total = sum(
            (w.brightness_range[1] - w.brightness_range[0]) +
            (w.contrast_range[1] - w.contrast_range[0])
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
            c_range = max(0, c_max - c_min)
            total_levels = b_range + c_range
            if total_levels <= 0:
                return

            level_step = int(round(total_levels * (abs_percent / 100.0)))
            if level_step <= 0:
                level_step = 1

            brightness = int(widget.b_slider.slider.value())
            contrast = int(widget.c_slider.slider.value())
            brightness = max(b_min, min(b_max, brightness))
            contrast = max(c_min, min(c_max, contrast))

            if brightness <= b_min:
                current_units = max(0, min(c_range, contrast - c_min))
            else:
                current_units = c_range + max(0, min(b_range, brightness - b_min))

            new_units = max(0, min(total_levels, current_units + sign * level_step))

            if new_units <= c_range:
                new_contrast = c_min + new_units
                new_brightness = b_min
            else:
                new_contrast = c_max
                new_brightness = b_min + (new_units - c_range)

            link_value = int(round((new_units / total_levels) * 100))
            widget.link_slider.slider.blockSignals(True)
            widget.link_slider.slider.setValue(link_value)
            widget.link_slider.slider.blockSignals(False)
            widget.link_slider.value_label.setText(str(link_value))

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

    def _update_auto_adjust_info(self, monitor_index=None):
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
        self.shortcut_key1 = self.shortcut_key1_combo.currentText()
        self.shortcut_key2 = self.shortcut_key2_combo.currentText()
        self.apply_shortcut_to_hook()
        self.trigger_save()

    def apply_shortcut_to_hook(self):
        if self.global_hook is not None:
            self.global_hook.set_trigger_shortcut(self.shortcut_key1, self.shortcut_key2)

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
        # Step 快捷鍵：自動模式→修改目標亮度；非自動模式→直接修改背光
        if self.auto_adjust_enabled:
            self.adjust_auto_adjust_target(delta * self.get_step_value())
        else:
            self.adjust_global_link(delta * self.get_step_value())

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
        """ 統一更新介面、圖示與所有螢幕的聯動值 """
        if self._updating_global_link:
            return

        value = int(self.snap_to_step(value))

        self._updating_global_link = True
        try:
            for wrapper, w in zip(self.monitor_wrappers, self.monitor_widgets):
                if not getattr(wrapper, "available", False):
                    continue
                try:
                    w.link_slider.slider.blockSignals(True)
                    w.link_slider.slider.setValue(value)
                    w.link_slider.slider.blockSignals(False)
                    w.link_slider.value_label.setText(str(value))
                    w.on_link(value) # 確保發送 DDC 指令
                except RuntimeError:
                    # widget 已被刪除（熱插拔後），跳過
                    pass
            self._sync_global_link_from_available_monitors()
            self._update_auto_adjust_info()
            self._broadcast_monitor_state_if_server_enabled()
            self._sync_app_state_to_remote_servers(global_link=value)
        finally:
            self._updating_global_link = False

    def toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self.show_main_page()

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
        data = {
            "known_monitor_names": self._known_monitor_names,
            "global_link": self.global_link_value,
            "step": self.get_step_value(),
            "auto_start": self.auto_start_enabled,
            "shortcut": {
                "key1": self.shortcut_key1,
                "key2": self.shortcut_key2,
            },
            "level_shortcuts": self.level_shortcuts,
            "auto_adjust": {
                "enabled": self.auto_adjust_enabled,
                "target": self.auto_adjust_target,
                "threshold": self.auto_adjust_threshold,
                "weight": self.auto_adjust_weight,
                "capture_interval": self.auto_adjust_capture_interval,
                "step_percent": self.auto_adjust_step_percent,
                "resource_saving_enabled": self.auto_adjust_resource_saving_enabled,
                "resource_saving_idle_seconds": self.auto_adjust_resource_saving_idle_seconds,
            },
            "network": {
                "server_enabled": self._network_server_enabled,
                "client_enabled": self._network_client_enabled,
            },
            "monitors": [],
        }
        for wrapper in self.monitor_wrappers:
            data["monitors"].append({
                "b_range": wrapper.brightness_range,
                "c_range": wrapper.contrast_range
            })

        # 無螢幕時保留既有設定，避免重啟後螢幕範圍遺失
        if not data["monitors"]:
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    old = json.load(f)
                    old_monitors = old.get("monitors", [])
                    if old_monitors:
                        data["monitors"] = old_monitors
            except Exception:
                pass

        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
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
            shortcut = data.get("shortcut", {}) if isinstance(data, dict) else {}
            saved_level_shortcuts = data.get("level_shortcuts", [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]) if isinstance(data, dict) else [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
            saved_key1 = shortcut.get("key1", "Alt") if isinstance(shortcut, dict) else "Alt"
            saved_key2 = shortcut.get("key2", "Win") if isinstance(shortcut, dict) else "Win"
            monitors_data = data.get("monitors", data) if isinstance(data, dict) else data
            auto_adjust_data = data.get("auto_adjust", {}) if isinstance(data, dict) else {}
            net_data = data.get("network", {}) if isinstance(data, dict) else {}

            # 載入網路功能設定（先存值，等 UI 建立後再啟動 server/client）
            self._network_server_enabled = bool(net_data.get("server_enabled", False))
            self._network_client_enabled = bool(net_data.get("client_enabled", False))

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

            if saved_key1 not in self.HOTKEY_OPTIONS:
                saved_key1 = "Alt"
            if saved_key2 not in self.HOTKEY_OPTIONAL_OPTIONS:
                saved_key2 = "Win"

            self.shortcut_key1_combo.blockSignals(True)
            self.shortcut_key2_combo.blockSignals(True)
            self.shortcut_key1_combo.setCurrentText(saved_key1)
            self.shortcut_key2_combo.setCurrentText(saved_key2)
            self.shortcut_key1_combo.blockSignals(False)
            self.shortcut_key2_combo.blockSignals(False)

            self.shortcut_key1 = saved_key1
            self.shortcut_key2 = saved_key2
            self.apply_shortcut_to_hook()

            self.clear_shortcut_rows()
            for shortcut_item in saved_level_shortcuts:
                self.add_shortcut_row(shortcut_item)
            if not self.shortcut_rows:
                for shortcut_item in DEFAULT_LEVEL_SHORTCUTS:
                    self.add_shortcut_row(shortcut_item)
            self.level_shortcuts = self.get_level_shortcuts()
            self.apply_level_shortcuts_to_hook()

            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(bool(saved_auto_start))
            self.autostart_checkbox.blockSignals(False)
            self.auto_start_enabled = bool(saved_auto_start)
            self.set_startup_enabled(self.auto_start_enabled)

            for wrapper, monitor_widget, range_widget, data_item in zip(self.monitor_wrappers, self.monitor_widgets, self.monitor_range_widgets, monitors_data[:len(self.monitor_wrappers)]):
                b_range = data_item.get("b_range", wrapper.brightness_range)
                c_range = data_item.get("c_range", wrapper.contrast_range)
                wrapper.brightness_range = list(b_range)
                wrapper.contrast_range = list(c_range)
                range_widget.set_ranges(wrapper.brightness_range, wrapper.contrast_range, emit_signal=False)
                monitor_widget.set_ranges(wrapper.brightness_range, wrapper.contrast_range)

            # 載入畫面自動調整設定
            self.auto_adjust_target = int(auto_adjust_data.get("target", 50))
            self.auto_adjust_threshold = int(auto_adjust_data.get("threshold", 5))
            self.auto_adjust_weight = float(auto_adjust_data.get("weight", AUTO_BRIGHTNESS_WEIGHT_DEFAULT))
            self.auto_adjust_capture_interval = float(auto_adjust_data.get("capture_interval", 1.0))
            self.auto_adjust_step_percent = float(auto_adjust_data.get("step_percent", 0.5))
            self.auto_adjust_resource_saving_enabled = bool(auto_adjust_data.get("resource_saving_enabled", True))
            self.auto_adjust_resource_saving_idle_seconds = float(auto_adjust_data.get("resource_saving_idle_seconds", 5.0))
            self.auto_adjust_capture_interval = max(0.1, min(5.0, self.auto_adjust_capture_interval))
            self.auto_adjust_step_percent = max(0.01, min(100.0, self.auto_adjust_step_percent))
            self.auto_adjust_resource_saving_idle_seconds = max(0.1, min(60.0, self.auto_adjust_resource_saving_idle_seconds))
            self.auto_adjust_checkbox.blockSignals(True)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.blockSignals(True)
            self.auto_adjust_threshold_spin.blockSignals(True)
            self.auto_adjust_weight_spin.blockSignals(True)
            self.auto_adjust_capture_interval_spin.blockSignals(True)
            self.auto_adjust_step_percent_spin.blockSignals(True)
            self.auto_adjust_resource_saving_checkbox.blockSignals(True)
            self.auto_adjust_resource_saving_idle_spin.blockSignals(True)
            self.auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            self.auto_adjust_threshold_spin.setValue(self.auto_adjust_threshold)
            self.auto_adjust_weight_spin.setValue(self.auto_adjust_weight)
            self.auto_adjust_capture_interval_spin.setValue(self.auto_adjust_capture_interval)
            self.auto_adjust_step_percent_spin.setValue(self.auto_adjust_step_percent)
            self.auto_adjust_resource_saving_checkbox.setChecked(self.auto_adjust_resource_saving_enabled)
            self.auto_adjust_resource_saving_idle_spin.setValue(self.auto_adjust_resource_saving_idle_seconds)
            self.auto_adjust_checkbox.blockSignals(False)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.blockSignals(False)
            self.auto_adjust_threshold_spin.blockSignals(False)
            self.auto_adjust_weight_spin.blockSignals(False)
            self.auto_adjust_capture_interval_spin.blockSignals(False)
            self.auto_adjust_step_percent_spin.blockSignals(False)
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

            self.update_auto_adjust_controls_visibility()
            self._update_auto_adjust_info()
        except FileNotFoundError:
            self.clear_shortcut_rows()
            for shortcut_item in DEFAULT_LEVEL_SHORTCUTS:
                self.add_shortcut_row(shortcut_item)
            self.level_shortcuts = self.get_level_shortcuts()
            self.apply_level_shortcuts_to_hook()
            self.sync_ui_with_current_monitor_levels()
            self.update_auto_adjust_controls_visibility()
            self.refresh_tray_display()
            # 首次啟動時立即建立預設設定檔，避免使用者找不到檔案。
            self.save_settings()
        except Exception as e:
            print("Load Error:", e)
        finally:
            self._loading_settings = False

        # 啟動網路功能（需在 load_settings 完成後，確保 UI checkbox 已存在）
        if self._network_server_enabled:
            self._net_server.start()
            if hasattr(self, "net_server_checkbox"):
                self.net_server_checkbox.setChecked(True)
        if self._network_client_enabled:
            self._net_client.start()
            if hasattr(self, "net_client_checkbox"):
                self.net_client_checkbox.setChecked(True)


# =========================
# Main
# =========================
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
