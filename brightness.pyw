import sys
import json
import threading
import ctypes
import os
from ctypes import wintypes

# 避免 Windows 上 Qt 輸出 DPI awareness 的無害警告
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

from PyQt6 import QtWidgets, QtCore, QtGui
from monitorcontrol import get_monitors

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

# 可選：指定 VapourSynth 腳本 (.vpy) 後，優先使用該管線抓取畫面亮度
# PowerShell 範例：$env:BRIGHTNESS_VS_SCRIPT='C:\path\to\source.vpy'
VAPOURSYNTH_SCRIPT_PATH = os.environ.get("BRIGHTNESS_VS_SCRIPT", "").strip()

# UDP IPC：test.vpy 每幀把亮度值 (float32) 發到此 port
VS_UDP_PORT = int(os.environ.get("BRIGHTNESS_VS_UDP_PORT", "57321"))

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

                # 檢查滑鼠按鍵是否匹配 level shortcuts
                mouse_vk = {
                    self.WM_LBUTTONDOWN: 0x01,   # VK_LBUTTON
                    self.WM_RBUTTONDOWN: 0x02,   # VK_RBUTTON
                    self.WM_MBUTTONDOWN: 0x04,   # VK_MBUTTON
                }.get(msg)
                if mouse_vk is None and msg == self.WM_XBUTTONDOWN:
                    ms = ctypes.cast(lParam, ctypes.POINTER(self.MSLLHOOKSTRUCT)).contents
                    xbtn = (ms.mouseData >> 16) & 0xFFFF
                    mouse_vk = {0x0001: 0x05, 0x0002: 0x06}.get(xbtn)  # XBUTTON1→0x05, XBUTTON2→0x06

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

                # 原有的滾輪處理
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
    def __init__(self, monitor, lock, brightness=None, contrast=None):
        super().__init__()
        self.monitor = monitor
        self.lock = lock
        self.brightness = brightness
        self.contrast = contrast

    def run(self):
        try:
            # 同一台螢幕序列化送出 DDC 指令，避免 context manager 狀態互相覆蓋
            with self.lock:
                with self.monitor as m:
                    if self.brightness is not None:
                        m.set_luminance(int(self.brightness))
                    if self.contrast is not None:
                        m.set_contrast(int(self.contrast))
        except Exception as e:
            print("DDC Error:", e)

# =========================
# Monitor Wrapper
# =========================
class MonitorWrapper:
    def __init__(self, monitor, index):
        self.monitor = monitor
        self.lock = threading.Lock()
        self.index = index
        self.brightness_range = [0, 100]
        self.contrast_range = [0, 100]
        self.supported = False
        self.brightness_supported = False
        self.contrast_supported = False
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
                    brightness = int(monitor.get_luminance())
                    self.brightness_supported = True
                except Exception:
                    brightness = None
                try:
                    contrast = int(monitor.get_contrast())
                    self.contrast_supported = True
                except Exception:
                    contrast = None

                self.supported = self.brightness_supported or self.contrast_supported
        except Exception:
            pass
        self.name = get_monitor_display_name(monitor, index, caps)

    def read_current_levels(self):
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

        if brightness is not None:
            b_min, b_max = self.brightness_range
            brightness = max(b_min, min(b_max, brightness))
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

        layout = QtWidgets.QVBoxLayout()
        # 讓版面更加緊湊
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)

        # ===== Sliders =====
        self.b_slider = self.create_slider("Brightness")
        self.c_slider = self.create_slider("Contrast")
        self.link_slider = self.create_slider("Link")

        self.b_slider.slider.valueChanged.connect(self.on_brightness)
        self.c_slider.slider.valueChanged.connect(self.on_contrast)
        self.link_slider.slider.valueChanged.connect(self.on_link)

        layout.addWidget(self.b_slider.widget)
        layout.addWidget(self.c_slider.widget)
        layout.addWidget(self.link_slider.widget)

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

    def on_brightness(self, v):
        self.pending_brightness = v
        self.restart()

    def on_contrast(self, v):
        self.pending_contrast = v
        self.restart()

    def on_link(self, percent):
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
        worker = DDCWorker(self.monitor.monitor, self.monitor.lock, self.pending_brightness, self.pending_contrast)
        self.threadpool.start(worker)


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
class _UdpLuminanceServer:
    """
    在背景執行緒監聽 UDP，接收 test.vpy 每幀送來的亮度值（4-byte float）。
    每次 get_average_and_reset() 返回自上次呼叫以來所有樣本的平均值並清空緩衝。
    若該區間內沒有收到任何封包則返回 None（可回退到 DXGI）。
    """
    _instance: "_UdpLuminanceServer | None" = None
    _lock = threading.Lock()

    def __init__(self, port: int = VS_UDP_PORT):
        import socket, struct
        self._struct = struct
        self._samples: list = []          # 區間內累積的所有樣本
        self._data_lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        self._sock.bind(("127.0.0.1", port))
        self._thread = threading.Thread(target=self._run, daemon=True, name="UdpLuminance")
        self._thread.start()
        print(f"[UdpLuminance] 監聽 127.0.0.1:{port}")

    def _run(self):
        while True:
            try:
                data, _ = self._sock.recvfrom(16)
                if len(data) >= 4:
                    val = self._struct.unpack("f", data[:4])[0]
                    with self._data_lock:
                        self._samples.append(float(val))
            except TimeoutError:
                pass  # settimeout 超時是正常的，繼續等待下一幀
            except OSError:
                break  # socket 真正關閉（程式退出）才結束執行緒
            except Exception:
                pass

    def get_average_and_reset(self) -> float | None:
        """返回自上次呼叫以來的樣本平均值，並清空列表。無資料則返回 None。"""
        with self._data_lock:
            if not self._samples:
                return None
            avg = sum(self._samples) / len(self._samples)
            self._samples = []
            return avg

    @classmethod
    def instance(cls) -> "_UdpLuminanceServer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


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

    _dxgi_camera = None
    _dxgi_lock = threading.Lock()
    _dxgi_disabled = False
    _vs_capture = None
    _vs_lock = threading.Lock()
    _vs_disabled = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.use_dxgi = HAS_DXGI and HAS_NUMPY
        self.use_vapoursynth = HAS_VAPOURSYNTH and bool(VAPOURSYNTH_SCRIPT_PATH)
        # 啟動 UDP 監聽（僅第一次會真正建立，之後共用 singleton）
        try:
            _UdpLuminanceServer.instance()
        except Exception as e:
            print(f"[UdpLuminance] 啟動失敗: {e}")

    @classmethod
    def _get_dxgi_camera(cls):
        if not HAS_DXGI or cls._dxgi_disabled:
            return None
        with cls._dxgi_lock:
            if cls._dxgi_camera is None:
                cls._dxgi_camera = dxcam.create(output_color="RGB")
            return cls._dxgi_camera

    @classmethod
    def _disable_dxgi(cls):
        with cls._dxgi_lock:
            cls._dxgi_disabled = True
            cls._dxgi_camera = None

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
        try:
            camera = self._get_dxgi_camera()
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
            print(f"DXGI 截圖錯誤: {e}")
            self._disable_dxgi()
            self.use_dxgi = False
            return None

    def run(self):
        result = None
        source = "—"

        # 優先使用 UDP 區間平均（涵蓋自上次截圖以來的所有幀）
        # 即使目前在 DXGI 模式，只要 VPY 重新開始發送資料就會自動切回 VPY
        try:
            udp_avg = _UdpLuminanceServer.instance().get_average_and_reset()
            if udp_avg is not None:
                result = udp_avg
                source = "VPY"
        except Exception:
            pass

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

    def __init__(self, parent=None):
        super().__init__(parent)
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

        remaining = self._desired_ddc - self._current_ddc_float
        if abs(remaining) <= 1e-6:
            self._direction = 0
            self._adjust_timer.stop()
            return

        # 每 tick 依使用者設定百分比前進；接近目標時避免超調
        step = min(abs(remaining), self.adjust_step_percent)
        delta_percent = step if remaining > 0 else -step
        self._current_ddc_float = max(0.0, min(100.0, self._current_ddc_float + delta_percent))

        # 達到目標即停止
        if (self._direction > 0 and self._current_ddc_float >= self._desired_ddc) or (
            self._direction < 0 and self._current_ddc_float <= self._desired_ddc
        ):
            self._current_ddc_float = self._desired_ddc
            self._direction = 0
            self._adjust_timer.stop()

        self._current_ddc = int(round(self._current_ddc_float))
        self.adjust_requested.emit(delta_percent)


# =========================
# Main Window
# =========================
class MainWindow(QtWidgets.QWidget):
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
        self.global_link_value = 0
        self.step_value = 5.0
        self.shortcut_key1 = "Alt"
        self.shortcut_key2 = "Win"
        self.auto_start_enabled = False
        self.level_shortcuts = [dict(item) for item in DEFAULT_LEVEL_SHORTCUTS]
        self.global_hook = None
        self._loading_settings = False

        # 畫面自動調整
        self.auto_adjust_enabled = False
        self.auto_adjust_target = 50
        self.auto_adjust_threshold = 5
        self.auto_adjust_weight = AUTO_BRIGHTNESS_WEIGHT_DEFAULT
        self.auto_adjust_capture_interval = 1.0
        self.auto_adjust_step_percent = 0.5
        self.auto_adjust_resource_saving_enabled = True
        self.auto_adjust_resource_saving_idle_seconds = 5.0
        self.screen_analyzer = ScreenAnalyzer(self)
        self.screen_analyzer.set_capture_interval_seconds(self.auto_adjust_capture_interval)
        self.screen_analyzer.set_adjust_step_percent(self.auto_adjust_step_percent)
        self.screen_analyzer.set_resource_saving(
            self.auto_adjust_resource_saving_enabled,
            self.auto_adjust_resource_saving_idle_seconds,
        )
        self.screen_analyzer.adjust_requested.connect(self.on_screen_adjust_requested)
        self.screen_analyzer.luminance_updated.connect(self.on_luminance_updated)
        self.screen_analyzer.luminance_source_updated.connect(self._on_luminance_source_updated)
        self.screen_analyzer.start()
        self._last_avg_luminance = None
        self._last_luminance_source = "—"
        self.current_effective_brightness = 0.0

        wrappers = [MonitorWrapper(m, i) for i, m in enumerate(get_monitors())]
        self.monitor_wrappers = [wrapper for wrapper in wrappers if wrapper.supported]
        self.monitor_widgets = []
        self.monitor_range_widgets = []
        self.shortcut_rows = []
        self._update_analyzer_levels()

        # 防抖存檔 Timer (避免頻繁寫入硬碟)
        self.save_timer = QtCore.QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_settings)

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
        settings_button = QtWidgets.QPushButton("設定")
        settings_button.clicked.connect(self.show_settings_page)
        top_bar.addWidget(settings_button)
        layout.addLayout(top_bar)

        self.auto_target_group = QtWidgets.QGroupBox("自動調整目標亮度")
        auto_target_layout = QtWidgets.QHBoxLayout()
        auto_target_layout.setContentsMargins(6, 6, 6, 6)
        auto_target_layout.setSpacing(6)
        self.auto_target_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.auto_target_slider.setRange(0, 100)
        self.auto_target_slider.setValue(self.auto_adjust_target)
        self.auto_target_value_label = QtWidgets.QLabel(str(self.auto_adjust_target))
        self.auto_target_value_label.setMinimumWidth(30)
        self.auto_target_slider.valueChanged.connect(self.on_main_target_slider_changed)
        auto_target_layout.addWidget(QtWidgets.QLabel("Target"))
        auto_target_layout.addWidget(self.auto_target_slider)
        auto_target_layout.addWidget(self.auto_target_value_label)
        self.auto_target_group.setLayout(auto_target_layout)
        layout.addWidget(self.auto_target_group)

        if not self.monitor_wrappers:
            layout.addWidget(QtWidgets.QLabel("未偵測到可控制的螢幕"))

        for wrapper in self.monitor_wrappers:
            monitor_widget = MonitorWidget(wrapper, self.threadpool)
            monitor_widget.value_changed.connect(self.on_monitor_link_changed)
            self.monitor_widgets.append(monitor_widget)
            layout.addWidget(monitor_widget)

        self.main_auto_adjust_info_label = QtWidgets.QLabel("畫面亮度: -- | 背光: -- | 來源: --")
        self.main_auto_adjust_info_label.setWordWrap(True)
        self.main_auto_adjust_info_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.main_auto_adjust_info_label)

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

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_container = QtWidgets.QWidget()
        scroll_layout = QtWidgets.QVBoxLayout()
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(8)

        global_group = QtWidgets.QGroupBox("全局設定")
        global_grid = QtWidgets.QGridLayout()
        global_grid.setContentsMargins(6, 6, 6, 6)
        global_grid.setSpacing(6)

        self.step_combo = QtWidgets.QComboBox()
        STEP_OPTIONS = ["1", "2", "2.5", "4", "5","10"]
        self.step_combo.addItems(STEP_OPTIONS)
        # 找出最接近預設值的選項
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
        scroll_layout.addWidget(global_group)

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

        # ---- 畫面自動調整 ----
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

        self.auto_adjust_target_spin = QtWidgets.QSpinBox()
        self.auto_adjust_target_spin.setRange(0, 100)
        self.auto_adjust_target_spin.setValue(self.auto_adjust_target)
        self.auto_adjust_target_spin.setSuffix(" %")
        self.auto_adjust_target_spin.valueChanged.connect(self.on_auto_adjust_settings_changed)

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

        self.auto_adjust_info_label = QtWidgets.QLabel(
            "畫面平均亮度: -- | 背光亮度: -- | 當前亮度: -- | 目標亮度: --"
        )
        self.auto_adjust_info_label.setWordWrap(True)

        auto_grid.addWidget(self.auto_adjust_checkbox, 0, 0, 1, 4)
        auto_grid.addWidget(QtWidgets.QLabel("目標亮度"), 1, 0)
        auto_grid.addWidget(self.auto_adjust_target_spin, 1, 1)
        auto_grid.addWidget(QtWidgets.QLabel("截圖間隔"), 1, 2)
        auto_grid.addWidget(self.auto_adjust_capture_interval_spin, 1, 3)
        auto_grid.addWidget(QtWidgets.QLabel("反應門檻"), 2, 0)
        auto_grid.addWidget(self.auto_adjust_threshold_spin, 2, 1)
        auto_grid.addWidget(QtWidgets.QLabel("調整級距"), 2, 2)
        auto_grid.addWidget(self.auto_adjust_step_percent_spin, 2, 3)
        auto_grid.addWidget(self.auto_adjust_resource_saving_checkbox, 3, 0, 1, 2)
        auto_grid.addWidget(QtWidgets.QLabel("靜止門檻"), 3, 2)
        auto_grid.addWidget(self.auto_adjust_resource_saving_idle_spin, 3, 3)
        auto_grid.addWidget(QtWidgets.QLabel("背光權重"), 4, 0)
        auto_grid.addWidget(self.auto_adjust_weight_spin, 4, 1)
        auto_grid.addWidget(self.auto_adjust_info_label, 5, 0, 1, 4)
        auto_group.setLayout(auto_grid)
        scroll_layout.addWidget(auto_group)

        if not self.monitor_wrappers:
            scroll_layout.addWidget(QtWidgets.QLabel("未偵測到可控制的螢幕"))

        for wrapper, monitor_widget in zip(self.monitor_wrappers, self.monitor_widgets):
            range_widget = MonitorRangeWidget(wrapper)
            range_widget.ranges_changed.connect(monitor_widget.set_ranges)
            range_widget.ranges_changed.connect(lambda _b, _c: self.trigger_save())
            range_widget.ranges_changed.connect(lambda _b, _c: self._update_analyzer_levels())
            self.monitor_range_widgets.append(range_widget)
            scroll_layout.addWidget(range_widget)

        # ---- 快捷鍵區域放在最下面 ----
        scroll_layout.addWidget(wheel_group)
        scroll_layout.addWidget(shortcut_group)

        scroll_layout.addStretch()
        scroll_container.setLayout(scroll_layout)
        scroll.setWidget(scroll_container)
        layout.addWidget(scroll)
        page.setLayout(layout)
        return page

    def show_main_page(self):
        self.stack.setCurrentWidget(self.main_page)
        self.show()
        self.raise_()
        self.activateWindow()

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

        link_values = []
        for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
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

        self.global_link_value = int(round(sum(link_values) / len(link_values))) if link_values else 0
        self.screen_analyzer.set_current_ddc(self.global_link_value)
        self._update_auto_adjust_info()
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
        self.screen_analyzer.enabled = self.auto_adjust_enabled
        self.screen_analyzer.reset_dynamic_capture_interval()
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
        self.auto_adjust_target = int(self.auto_adjust_target_spin.value())
        self.auto_adjust_threshold = int(self.auto_adjust_threshold_spin.value())
        self.auto_adjust_weight = float(self.auto_adjust_weight_spin.value())
        self.auto_adjust_capture_interval = float(self.auto_adjust_capture_interval_spin.value())
        self.auto_adjust_step_percent = float(self.auto_adjust_step_percent_spin.value())
        self.auto_adjust_resource_saving_enabled = bool(self.auto_adjust_resource_saving_checkbox.isChecked())
        self.auto_adjust_resource_saving_idle_seconds = float(self.auto_adjust_resource_saving_idle_spin.value())
        self.screen_analyzer.target = self.auto_adjust_target
        self.screen_analyzer.threshold = self.auto_adjust_threshold
        self.screen_analyzer.weight = self.auto_adjust_weight
        self.screen_analyzer.set_capture_interval_seconds(self.auto_adjust_capture_interval)
        self.screen_analyzer.set_adjust_step_percent(self.auto_adjust_step_percent)
        self.screen_analyzer.set_resource_saving(
            self.auto_adjust_resource_saving_enabled,
            self.auto_adjust_resource_saving_idle_seconds,
        )
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(self.auto_adjust_target)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(self.auto_adjust_target))
        self._update_auto_adjust_info()
        self.trigger_save()

    def on_main_target_slider_changed(self, value):
        self.set_auto_adjust_target(value)

    def set_auto_adjust_target(self, value, trigger_save=True):
        value = max(0, min(100, self.snap_to_step(value)))
        value = int(round(value))
        self.auto_adjust_target = value
        self.screen_analyzer.target = value
        if hasattr(self, "auto_adjust_target_spin"):
            self.auto_adjust_target_spin.blockSignals(True)
            self.auto_adjust_target_spin.setValue(value)
            self.auto_adjust_target_spin.blockSignals(False)
        if hasattr(self, "auto_target_slider"):
            self.auto_target_slider.blockSignals(True)
            self.auto_target_slider.setValue(value)
            self.auto_target_slider.blockSignals(False)
            self.auto_target_value_label.setText(str(value))
        self._update_auto_adjust_info()
        if trigger_save:
            self.trigger_save()

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
        self.screen_analyzer.total_levels = max(1, total)

    def on_screen_adjust_requested(self, delta_percent):
        if not self.monitor_wrappers or not self.monitor_widgets:
            return

        try:
            delta_percent = float(delta_percent)
        except (TypeError, ValueError):
            return
        if abs(delta_percent) <= 1e-9:
            return

        sign = 1 if delta_percent > 0 else -1
        abs_percent = abs(delta_percent)

        link_values = []
        for wrapper, widget in zip(self.monitor_wrappers, self.monitor_widgets):
            b_min, b_max = wrapper.brightness_range
            c_min, c_max = wrapper.contrast_range
            b_range = max(0, b_max - b_min)
            c_range = max(0, c_max - c_min)
            total_levels = b_range + c_range
            if total_levels <= 0:
                continue

            level_step = int(round(total_levels * (abs_percent / 100.0)))
            if level_step <= 0:
                level_step = 1

            brightness = int(widget.b_slider.slider.value())
            contrast = int(widget.c_slider.slider.value())
            brightness = max(b_min, min(b_max, brightness))
            contrast = max(c_min, min(c_max, contrast))

            # 先走對比，再走亮度（與 Link 滑桿同邏輯）
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

            link_values.append(link_value)

        if link_values:
            self.global_link_value = int(round(sum(link_values) / len(link_values)))
            self.screen_analyzer.set_current_ddc(self.global_link_value)
            self._update_auto_adjust_info()

    def on_luminance_updated(self, lum):
        self._last_avg_luminance = float(lum)
        self._update_auto_adjust_info()

    def _on_luminance_source_updated(self, source: str):
        self._last_luminance_source = source
        self._update_auto_adjust_info()

    def _update_auto_adjust_info(self):
        avg = getattr(self, "_last_avg_luminance", None)
        source = getattr(self, "_last_luminance_source", "—")
        backlight = float(self.global_link_value)
        target = float(self.auto_adjust_target)
        weight = float(self.auto_adjust_weight)
        if avg is None:
            self.current_effective_brightness = backlight
            detail_text = (
                f"畫面亮度: -- | 背光: {backlight:.1f}% | "
                f"當前: -- | 目標: {target:.1f}% | 權重: {weight:.2f} | 來源: {source}"
            )
            short_text = f"畫面亮度: -- | 背光: {backlight:.1f}% | 來源: {source}"
        else:
            c = get_dynamic_content_coeff(avg)
            current = (avg * c + backlight * weight) / (c + weight)
            self.current_effective_brightness = current
            detail_text = (
                f"畫面亮度: {avg:.1f}% | "
                f"背光: {backlight:.1f}% | "
                f"當前: {current:.1f}% | "
                f"目標: {target:.1f}% | "
                f"權重: {weight:.2f} | 來源: {source}"
            )
            short_text = f"畫面亮度: {avg:.1f}% | 背光: {backlight:.1f}% | 當前: {current:.1f}% | 來源: {source}"
        if hasattr(self, "auto_adjust_info_label"):
            self.auto_adjust_info_label.setText(detail_text)
        if hasattr(self, "main_auto_adjust_info_label"):
            self.main_auto_adjust_info_label.setText(short_text)
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
            self.tray.setToolTip(
                f"背光亮度: {self.global_link_value:.1f}%\n"
                f"當前亮度: {self.current_effective_brightness:.1f}%\n"
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
        self.update_global_link(value)
        self.trigger_save()

    def update_global_link(self, value):
        """ 統一更新介面、圖示與所有螢幕的聯動值 """
        if self._updating_global_link:
            return

        value = int(self.snap_to_step(value))

        self._updating_global_link = True
        self.global_link_value = value
        self.screen_analyzer.set_current_ddc(value)
        self._update_auto_adjust_info()
        try:
            for w in self.monitor_widgets:
                w.link_slider.slider.blockSignals(True)
                w.link_slider.slider.setValue(value)
                w.link_slider.slider.blockSignals(False)
                w.link_slider.value_label.setText(str(value))
                w.on_link(value) # 確保發送 DDC 指令
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
        self.screen_analyzer.stop()
        self._is_quitting = True
        QtWidgets.QApplication.quit()

    def trigger_save(self):
        if self._loading_settings:
            return
        # 重新計時，延遲 500ms 後才執行寫入，避免拉動滑桿時瘋狂存檔
        self.save_timer.start(200)

    def save_settings(self):
        data = {
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
            "monitors": [],
        }
        for wrapper in self.monitor_wrappers:
            data["monitors"].append({
                "b_range": wrapper.brightness_range,
                "c_range": wrapper.contrast_range
            })
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

            for wrapper, monitor_widget, range_widget, data_item in zip(self.monitor_wrappers, self.monitor_widgets, self.monitor_range_widgets, monitors_data):
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
            self.auto_adjust_target_spin.blockSignals(True)
            self.auto_adjust_threshold_spin.blockSignals(True)
            self.auto_adjust_weight_spin.blockSignals(True)
            self.auto_adjust_capture_interval_spin.blockSignals(True)
            self.auto_adjust_step_percent_spin.blockSignals(True)
            self.auto_adjust_resource_saving_checkbox.blockSignals(True)
            self.auto_adjust_resource_saving_idle_spin.blockSignals(True)
            self.auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.setChecked(self.auto_adjust_enabled)
            self.auto_adjust_target_spin.setValue(self.auto_adjust_target)
            self.auto_adjust_threshold_spin.setValue(self.auto_adjust_threshold)
            self.auto_adjust_weight_spin.setValue(self.auto_adjust_weight)
            self.auto_adjust_capture_interval_spin.setValue(self.auto_adjust_capture_interval)
            self.auto_adjust_step_percent_spin.setValue(self.auto_adjust_step_percent)
            self.auto_adjust_resource_saving_checkbox.setChecked(self.auto_adjust_resource_saving_enabled)
            self.auto_adjust_resource_saving_idle_spin.setValue(self.auto_adjust_resource_saving_idle_seconds)
            self.auto_adjust_checkbox.blockSignals(False)
            if hasattr(self, "main_auto_adjust_checkbox"):
                self.main_auto_adjust_checkbox.blockSignals(False)
            self.auto_adjust_target_spin.blockSignals(False)
            self.auto_adjust_threshold_spin.blockSignals(False)
            self.auto_adjust_weight_spin.blockSignals(False)
            self.auto_adjust_capture_interval_spin.blockSignals(False)
            self.auto_adjust_step_percent_spin.blockSignals(False)
            self.auto_adjust_resource_saving_checkbox.blockSignals(False)
            self.auto_adjust_resource_saving_idle_spin.blockSignals(False)
            self.screen_analyzer.enabled = self.auto_adjust_enabled
            self.set_auto_adjust_target(self.auto_adjust_target, trigger_save=False)
            self.screen_analyzer.threshold = self.auto_adjust_threshold
            self.screen_analyzer.weight = self.auto_adjust_weight
            self.screen_analyzer.set_capture_interval_seconds(self.auto_adjust_capture_interval)
            self.screen_analyzer.set_adjust_step_percent(self.auto_adjust_step_percent)
            self.screen_analyzer.set_resource_saving(
                self.auto_adjust_resource_saving_enabled,
                self.auto_adjust_resource_saving_idle_seconds,
            )

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


# =========================
# Main
# =========================
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())