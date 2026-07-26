import sys
import threading
import ctypes
import os
import time
from ctypes import wintypes
from typing import Optional

from PyQt6 import QtCore


# =========================
# 跨執行緒 COM 初始化 (WMI/comtypes)
# =========================
_comtypes_mod = None

def _get_comtypes():
    global _comtypes_mod
    if _comtypes_mod is None:
        try:
            import comtypes as _ct
            _comtypes_mod = _ct
        except ImportError:
            _comtypes_mod = False
    return _comtypes_mod if _comtypes_mod is not False else None


def _com_init():
    """在目前執行緒初始化 COM (僅第一次有效)。"""
    ct = _get_comtypes()
    if ct:
        try:
            ct.CoInitialize()
        except Exception:
            pass


def _com_uninit():
    """在目前執行緒解除 COM 初始化。"""
    ct = _get_comtypes()
    if ct:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


# =========================
# DDC 逾時包裝
# =========================
def run_ddc_with_timeout(func, timeout_sec: float = 3.0, default=None):
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


# =========================
# WMI 亮度控制（筆電內建螢幕備援）
# =========================
try:
    import wmi as wmi_mod
    HAS_WMI = True
except ImportError:
    wmi_mod = None
    HAS_WMI = False


def wmi_brightness_supported():
    if not HAS_WMI:
        return False
    _com_init()
    try:
        conn = wmi_mod.WMI(namespace="WMI")
        methods = list(conn.WmiMonitorBrightnessMethods())
        monitors = list(conn.WmiMonitorBrightness())
        return bool(methods and monitors)
    except Exception:
        return False


def wmi_set_brightness(value):
    if not HAS_WMI:
        return False
    _com_init()
    try:
        conn = wmi_mod.WMI(namespace="WMI")
        percent = int(max(0, min(100, value)))
        for method in conn.WmiMonitorBrightnessMethods():
            method.WmiSetBrightness(percent, 0)
        return True
    except Exception:
        return False


def wmi_get_brightness():
    if not HAS_WMI:
        return None
    _com_init()
    try:
        conn = wmi_mod.WMI(namespace="WMI")
        monitors = list(conn.WmiMonitorBrightness())
        if monitors:
            value = getattr(monitors[0], "CurrentBrightness", None)
            return int(value) if value is not None else None
    except Exception:
        return None


# =========================
# 螢幕名稱解析輔助
# =========================
try:
    import winreg
except ImportError:
    winreg = None

# DDC 容錯常數
DDC_WRITE_FAILURES_BEFORE_COOLDOWN = 3
DDC_WRITE_COOLDOWN_SECONDS = 30.0
DDC_WRITE_COOLDOWN_MAX_SECONDS = 300.0
DDC_VCP_WRITE_GAP_SECONDS = 0.01


def _rect_to_tuple(rect):
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


def get_windows_active_display_entries():
    """Return visible desktop display entries ordered primary first, then by position."""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    entries = []
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
            entries.append({
                "hmonitor": hmonitor,
                "display_name": str(info.szDevice),
                "rect": _rect_to_tuple(info.rcMonitor),
                "work": _rect_to_tuple(info.rcWork),
                "primary": bool(info.dwFlags & 1),
            })
        return True

    try:
        user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(enum_proc), 0)
    except Exception:
        return []
    entries.sort(key=lambda e: (0 if e["primary"] else 1, e["rect"][1], e["rect"][0]))
    return entries


def get_monitor_device_id(display_name):
    """Return DeviceID for a display name using EnumDisplayDevicesW."""
    if sys.platform != "win32":
        return ""
    try:
        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        DISPLAY_DEVICE_ACTIVE = 0x00000001
        user32 = ctypes.windll.user32
        for monitor_idx in range(16):
            monitor = DISPLAY_DEVICEW()
            monitor.cb = ctypes.sizeof(monitor)
            if not user32.EnumDisplayDevicesW(display_name, monitor_idx, ctypes.byref(monitor), 0):
                continue
            if int(monitor.StateFlags) & DISPLAY_DEVICE_ACTIVE:
                return str(monitor.DeviceID)
    except Exception:
        pass
    return ""


def monitor_name_from_device_id(device_id):
    """Query registry EDID to get the human-readable monitor name."""
    if winreg is None or not device_id:
        return None
    try:
        parts = str(device_id).split("\\")
        if len(parts) < 3:
            return None
        vendor = parts[1]
        instance = parts[2].split("&UID", 1)[0]
        base = rf"SYSTEM\CurrentControlSet\Enum\DISPLAY\{vendor}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as vendor_key:
            candidates = []
            for idx in range(64):
                try:
                    candidates.append(winreg.EnumKey(vendor_key, idx))
                except OSError:
                    break
        candidates.sort(key=lambda item: 0 if item.lower() == instance.lower() else 1)
        for candidate in candidates:
            try:
                path = rf"{base}\{candidate}\Device Parameters"
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as params_key:
                    edid, _typ = winreg.QueryValueEx(params_key, "EDID")
                name = parse_edid_monitor_name(bytes(edid))
                if name:
                    return name
            except Exception:
                continue
    except Exception:
        pass
    return None


def parse_edid_monitor_name(edid: bytes) -> Optional[str]:
    """從 EDID 解析螢幕名稱（Descriptor Block 類型 0xFC 為 Monitor Name）。"""
    try:
        if len(edid) < 128:
            return None
        for offset in range(54, 126, 18):
            tag = edid[offset + 3]
            if tag == 0xFC:
                raw = edid[offset + 5 : offset + 18]
                name = raw.decode("utf-8", errors="replace").strip().rstrip("\n").strip()
                if name and is_valid_monitor_name(name):
                    return name
    except Exception:
        pass
    return None


def is_valid_monitor_name(name: str) -> bool:
    """檢查字串是否像有效的螢幕名稱（拒絕原始 VCP capabilities 文字）。"""
    if not name or len(name) < 2 or len(name) > 100:
        return False
    suspicious = ("prot(", "type(", "model(", "vcp(", "cmds(", "mccs_ver")
    if any(s in name.lower() for s in suspicious):
        return False
    for ch in name:
        if ord(ch) < 32 or ord(ch) == 127:
            return False
    return True


# =========================
# Windows 實體螢幕列舉（DXVA2 DDC/CI）
# =========================

class WinPhysicalMonitor:
    """Small wrapper around Windows DXVA2 DDC/CI APIs."""

    def __init__(self, handle, description="", display_name="", device_id="", index=0):
        self.handle = handle
        self.description = str(description or "").strip()
        self.display_name = str(display_name or "").strip()
        self.device_id = str(device_id or "").strip()
        self.index = int(index)
        self._closed = False
        self.name = self._resolve_name()

    def _resolve_name(self):
        name = monitor_name_from_device_id(self.device_id)
        if is_valid_monitor_name(name):
            return name
        if is_valid_monitor_name(self.description):
            return self.description
        return f"Display {self.index + 1}"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        if self._closed or not self.handle:
            return
        try:
            dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
            dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]
            dxva2.DestroyPhysicalMonitor.restype = wintypes.BOOL
            dxva2.DestroyPhysicalMonitor(self.handle)
        except Exception:
            pass
        self._closed = True
        self.handle = None

    def get_vcp_capabilities(self):
        result = {"model": self.name, "vcp": {}}
        caps_text = self._read_capabilities_string()
        if caps_text:
            result["raw"] = caps_text
            model = self._parse_capability_model(caps_text)
            if is_valid_monitor_name(model):
                result["model"] = model
                self.name = model
            if " 10" in caps_text or "(10" in caps_text or "vcp(10" in caps_text.lower():
                result["vcp"][0x10] = True
            if " 12" in caps_text or "(12" in caps_text or "vcp(12" in caps_text.lower():
                result["vcp"][0x12] = True

        for code in (0x10, 0x12):
            try:
                self._get_vcp(code)
                result["vcp"][code] = True
            except Exception:
                pass
        return result

    def _read_capabilities_string(self):
        if sys.platform != "win32":
            return ""
        try:
            dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
            length = wintypes.DWORD(0)
            if not dxva2.GetCapabilitiesStringLength(self.handle, ctypes.byref(length)):
                return ""
            if length.value <= 1:
                return ""
            buf = ctypes.create_string_buffer(length.value)
            if not dxva2.CapabilitiesRequestAndCapabilitiesReply(self.handle, buf, length.value):
                return ""
            return buf.value.decode("ascii", errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _parse_capability_model(caps_text):
        lower = caps_text.lower()
        marker = "model("
        start = lower.find(marker)
        if start < 0:
            return None
        start += len(marker)
        end = caps_text.find(")", start)
        if end < 0:
            return None
        return caps_text[start:end].strip()

    def _get_vcp(self, code):
        dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
        vcp_type = wintypes.DWORD(0)
        current = wintypes.DWORD(0)
        maximum = wintypes.DWORD(0)
        ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
            self.handle,
            wintypes.BYTE(int(code)),
            ctypes.byref(vcp_type),
            ctypes.byref(current),
            ctypes.byref(maximum),
        )
        if not ok:
            raise OSError(ctypes.get_last_error())
        return int(current.value), int(maximum.value)

    def _set_vcp(self, code, value):
        dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
        ok = dxva2.SetVCPFeature(self.handle, wintypes.BYTE(int(code)), wintypes.DWORD(int(value)))
        if not ok:
            raise OSError(ctypes.get_last_error())

    def get_luminance(self):
        return self._get_vcp(0x10)[0]

    def set_luminance(self, value):
        self._set_vcp(0x10, value)

    def get_contrast(self):
        return self._get_vcp(0x12)[0]

    def set_contrast(self, value):
        self._set_vcp(0x12, value)


def get_windows_physical_monitors():
    if sys.platform != "win32":
        return []
    try:
        dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
        dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

        monitors = []
        for entry in get_windows_active_display_entries():
            hmonitor = entry.get("hmonitor")
            display_name = entry.get("display_name", "")
            if not hmonitor:
                continue
            count = wintypes.DWORD(0)
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)):
                continue
            if count.value <= 0:
                continue
            arr_type = PHYSICAL_MONITOR * count.value
            arr = arr_type()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count, arr):
                continue
            device_id = get_monitor_device_id(display_name)
            for physical_idx in range(count.value):
                physical = arr[physical_idx]
                monitors.append(
                    WinPhysicalMonitor(
                        physical.hPhysicalMonitor,
                        physical.szPhysicalMonitorDescription,
                        display_name=display_name,
                        device_id=device_id,
                        index=len(monitors),
                    )
                )

        return monitors
    except Exception as e:
        print(f"[DDC] DXVA2 physical monitor scan error: {e}")
        return []


def get_local_ddc_monitors():
    return get_windows_physical_monitors()


def get_windows_active_monitor_names():
    """Return active monitor names reported by Windows, independent of DDC support."""
    names = []

    def add_name(name):
        if is_valid_monitor_name(name) and name not in names:
            names.append(name)

    for entry in get_windows_active_display_entries():
        display_name = entry.get("display_name", "")
        device_id = get_monitor_device_id(display_name)
        add_name(monitor_name_from_device_id(device_id) or display_name)

    return names


# =========================
# MonitorWrapper
# =========================

class MonitorWrapper:
    def __init__(self, monitor=None, index=0, name="", b_range=None, c_range=None):
        self.monitor = monitor
        self.lock = threading.Lock()
        self.index = index
        self.brightness_range = list(b_range or [0, 100])
        self.contrast_range = list(c_range or [0, 100])
        self.supported = False
        self.brightness_supported = False
        self.contrast_supported = False
        self.available = False
        self.wmi_supported = wmi_brightness_supported()
        self.name = name or f"Display {index + 1}"
        self._cached_brightness: Optional[int] = None
        self._cached_contrast: Optional[int] = None
        self._ddc_write_error_count = 0
        self._ddc_write_cooldown_until = 0.0
        self._ddc_write_state_lock = threading.Lock()

        if monitor is None:
            if name and not is_valid_monitor_name(self.name):
                self.name = f"Display {index + 1}"
            return

        self.name = monitor.name
        self.supported = True
        self.available = True

        # 用 get_luminance() 測試 DDC 可用性
        # 成功 → DDC 螢幕；失敗（如虛擬螢幕）→ WMI 備援
        ddc_ok = False
        try:
            with self.lock:
                with monitor as m:
                    val = int(m.get_luminance())
                    self._cached_brightness = val
                    self.brightness_supported = True
                    try:
                        self._cached_contrast = int(m.get_contrast())
                        self.contrast_supported = True
                    except Exception:
                        pass
                    ddc_ok = True
        except Exception:
            pass

        if ddc_ok:
            return

        # DDC 失敗 → WMI 備援
        if self.wmi_supported:
            self.brightness_supported = True
            self.contrast_supported = False
        else:
            self.supported = False
            self.available = False

    def can_write_ddc(self):
        if not self.available or self.monitor is None:
            return False
        with self._ddc_write_state_lock:
            return time.monotonic() >= self._ddc_write_cooldown_until

    def record_ddc_write_success(self):
        with self._ddc_write_state_lock:
            self._ddc_write_error_count = 0
            self._ddc_write_cooldown_until = 0.0

    def record_ddc_write_failure(self, error):
        with self._ddc_write_state_lock:
            self._ddc_write_error_count += 1
            if self._ddc_write_error_count < DDC_WRITE_FAILURES_BEFORE_COOLDOWN:
                return

            cooldown = min(
                DDC_WRITE_COOLDOWN_MAX_SECONDS,
                DDC_WRITE_COOLDOWN_SECONDS * (2 ** max(0, self._ddc_write_error_count - DDC_WRITE_FAILURES_BEFORE_COOLDOWN)),
            )
            self._ddc_write_cooldown_until = time.monotonic() + cooldown
        print(f"[DDC] Write error on {self.name}: {error}; pause DDC writes for {int(cooldown)}s")

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
                brightness, contrast = run_ddc_with_timeout(
                    _read, timeout_sec=3.0, default=(None, None)
                )
        except Exception:
            pass

        if brightness is None:
            brightness = self._cached_brightness
        if contrast is None:
            contrast = self._cached_contrast

        if brightness is None:
            brightness = wmi_get_brightness()

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
# DDC 執行緒 Worker
# =========================

class DDCWorker(QtCore.QRunnable):
    """DDC 寫入工作單位，由 threadpool 排程執行。"""
    def __init__(self, wrapper, brightness=None, contrast=None, contrast_supported=True):
        super().__init__()
        self.wrapper = wrapper
        self.monitor = wrapper.monitor
        self.lock = wrapper.lock
        self.brightness = brightness
        self.contrast = contrast
        self.contrast_supported = contrast_supported

    def run(self):
        if not self.wrapper.available or not self.wrapper.can_write_ddc():
            return
        if self.monitor is None:
            self.wrapper.record_ddc_write_failure("monitor handle is not available")
            return

        desired_brightness = int(self.brightness) if self.brightness is not None else None
        desired_contrast = (
            int(self.contrast)
            if self.contrast_supported and self.contrast is not None
            else None
        )
        write_brightness = (
            desired_brightness is not None
            and desired_brightness != self.wrapper._cached_brightness
        )
        write_contrast = (
            desired_contrast is not None
            and desired_contrast != self.wrapper._cached_contrast
        )
        if not write_brightness and not write_contrast:
            return

        try:
            with self.lock:
                with self.monitor as m:
                    if write_brightness:
                        try:
                            m.set_luminance(desired_brightness)
                            self.wrapper._cached_brightness = desired_brightness
                        except Exception as e:
                            if wmi_set_brightness(desired_brightness):
                                self.wrapper._cached_brightness = desired_brightness
                                self.wrapper.record_ddc_write_success()
                            else:
                                self.wrapper.record_ddc_write_failure(f"VCP 0x10 brightness: {e}")
                            return
                    if write_brightness and write_contrast:
                        time.sleep(DDC_VCP_WRITE_GAP_SECONDS)
                    if write_contrast:
                        try:
                            m.set_contrast(desired_contrast)
                            self.wrapper._cached_contrast = desired_contrast
                        except Exception as e:
                            self.wrapper.record_ddc_write_failure(f"VCP 0x12 contrast: {e}")
                            return
            self.wrapper.record_ddc_write_success()
        except Exception as e:
            self.wrapper.record_ddc_write_failure(e)
            return


# =========================
# 背景亮度讀取 Worker
# =========================
class LevelReadSignals(QtCore.QObject):
    result = QtCore.pyqtSignal(object, object, object)


class LevelReadWorker(QtCore.QRunnable):
    def __init__(self, wrapper):
        super().__init__()
        self.wrapper = wrapper
        self.signals = LevelReadSignals()

    def run(self):
        brightness, contrast = self.wrapper.read_current_levels()
        self.signals.result.emit(self.wrapper, brightness, contrast)


# =========================
# 佔位螢幕補齊
# =========================

def append_missing_windows_display_placeholders(wrappers, preserved_ranges=None):
    """Add unavailable placeholders for active Windows displays absent from DDC results."""
    preserved_ranges = preserved_ranges or {}
    existing = {w.name for w in wrappers if is_valid_monitor_name(getattr(w, "name", ""))}
    for name in get_windows_active_monitor_names():
        if name in existing:
            continue
        b_range, c_range = preserved_ranges.get(name, ([0, 100], [0, 100]))
        placeholder = MonitorWrapper(
            monitor=None,
            index=len(wrappers),
            name=name,
            b_range=list(b_range),
            c_range=list(c_range),
        )
        placeholder.available = False
        placeholder.supported = False
        placeholder.brightness_supported = False
        placeholder.contrast_supported = False
        wrappers.append(placeholder)
        existing.add(name)
