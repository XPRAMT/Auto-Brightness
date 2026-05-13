import os
import sys
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, pyqtProperty, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine

from monitorcontrol import get_monitors


class BrightnessBackend(QObject):
    monitorsChanged = pyqtSignal()
    statusMessageChanged = pyqtSignal()
    globalBrightnessChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._monitors: list[dict[str, Any]] = []
        self._status_message = "初始化中..."
        self._global_brightness = 50

        # 避免拖動滑桿時每一步都同步寫 DDC/CI 造成 UI 卡頓
        self._pending_monitor_levels: dict[int, int] = {}
        self._pending_global_level: int | None = None
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(80)
        self._apply_timer.timeout.connect(self._flush_pending_updates)

        self.refreshMonitors()

    @pyqtProperty("QVariantList", notify=monitorsChanged)
    def monitors(self):
        return self._monitors

    @pyqtProperty(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @pyqtProperty(int, notify=globalBrightnessChanged)
    def globalBrightness(self) -> int:
        return self._global_brightness

    def _set_status(self, message: str) -> None:
        if self._status_message != message:
            self._status_message = message
            self.statusMessageChanged.emit()

    def _read_luminance(self, monitor_obj) -> int:
        try:
            value = monitor_obj.get_luminance()
            return int(max(0, min(100, value)))
        except Exception:
            return 50

    def _schedule_apply(self) -> None:
        self._apply_timer.start()

    def _update_local_monitor_value(self, index: int, brightness: int) -> None:
        changed = False
        for item in self._monitors:
            if item["index"] == index:
                if item.get("brightness") != brightness:
                    item["brightness"] = brightness
                    changed = True
                break
        if changed:
            self.monitorsChanged.emit()
            self._recompute_global_brightness()

    def _update_local_all_value(self, brightness: int) -> None:
        changed = False
        for item in self._monitors:
            if item.get("brightness") != brightness:
                item["brightness"] = brightness
                changed = True

        if changed:
            self.monitorsChanged.emit()

        if self._global_brightness != brightness:
            self._global_brightness = brightness
            self.globalBrightnessChanged.emit()

    def _recompute_global_brightness(self) -> None:
        if not self._monitors:
            return
        avg = round(sum(m["brightness"] for m in self._monitors) / len(self._monitors))
        if avg != self._global_brightness:
            self._global_brightness = int(avg)
            self.globalBrightnessChanged.emit()

    def _flush_pending_updates(self) -> None:
        pending_global = self._pending_global_level
        pending_monitor = dict(self._pending_monitor_levels)
        self._pending_global_level = None
        self._pending_monitor_levels.clear()

        if pending_global is not None:
            success_count = 0
            errors: list[str] = []
            try:
                for idx, monitor in enumerate(get_monitors()):
                    try:
                        with monitor:
                            monitor.set_luminance(pending_global)
                        success_count += 1
                    except Exception as exc:
                        errors.append(f"Display {idx + 1}: {exc}")
            except Exception as exc:
                self._set_status(f"設定全域亮度失敗：{exc}")
                return

            if errors and success_count == 0:
                self._set_status("全域設定失敗：" + " | ".join(errors[:2]))
            elif errors:
                self._set_status(f"已設定 {success_count} 台，部分失敗")
            else:
                self._set_status(f"全域亮度已設定為 {pending_global}%")
            return

        if pending_monitor:
            try:
                monitor_list = list(get_monitors())
                for index, brightness in pending_monitor.items():
                    if 0 <= index < len(monitor_list):
                        with monitor_list[index] as monitor:
                            monitor.set_luminance(brightness)
                self._set_status("已套用亮度調整")
            except Exception as exc:
                self._set_status(f"設定亮度失敗：{exc}")

    @pyqtSlot()
    def refreshMonitors(self) -> None:
        monitors: list[dict[str, Any]] = []
        try:
            for idx, monitor in enumerate(get_monitors()):
                with monitor:
                    name = getattr(monitor, "name", "") or f"Display {idx + 1}"
                    value = self._read_luminance(monitor)
                    monitors.append({
                        "index": idx,
                        "name": str(name),
                        "brightness": int(value),
                    })
        except Exception as exc:
            self._set_status(f"讀取螢幕失敗：{exc}")
            self._monitors = []
            self.monitorsChanged.emit()
            return

        self._monitors = monitors
        self.monitorsChanged.emit()

        if monitors:
            avg = round(sum(m["brightness"] for m in monitors) / len(monitors))
            self._global_brightness = int(avg)
            self.globalBrightnessChanged.emit()
            self._set_status(f"已偵測 {len(monitors)} 台螢幕")
        else:
            self._set_status("未偵測到支援 DDC/CI 的螢幕")

    @pyqtSlot(int, int)
    def setMonitorBrightness(self, index: int, brightness: int) -> None:
        brightness = int(max(0, min(100, brightness)))
        self._pending_global_level = None
        self._pending_monitor_levels[index] = brightness
        self._update_local_monitor_value(index, brightness)
        self._schedule_apply()

    @pyqtSlot(int)
    def setAllBrightness(self, brightness: int) -> None:
        brightness = int(max(0, min(100, brightness)))
        self._pending_monitor_levels.clear()
        self._pending_global_level = brightness
        self._update_local_all_value(brightness)
        self._schedule_apply()


if __name__ == "__main__":
    # Qt Quick 在 Windows 預設會走 GPU；這裡明確指定 DX11 與 threaded loop
    os.environ.setdefault("QSG_RHI_BACKEND", "d3d11")
    os.environ.setdefault("QSG_RENDER_LOOP", "threaded")

    app = QGuiApplication(sys.argv)

    backend = BrightnessBackend()

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)

    qml_path = os.path.join(os.path.dirname(__file__), "qml", "Main.qml")
    engine.load(qml_path)

    if not engine.rootObjects():
        sys.exit(1)

    sys.exit(app.exec())
