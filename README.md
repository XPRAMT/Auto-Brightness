# Auto Brightness

Auto Brightness is a Windows desktop utility for controlling monitor brightness and contrast from a compact PyQt6 tray app. It supports DDC/CI external displays, WMI brightness fallback for compatible built-in displays, global hotkeys, content-aware automatic brightness, and LAN-based remote monitor control.

## Features

- Adjust brightness and contrast for supported local monitors.
- Link all monitor brightness values with one global slider.
- Automatically adjust backlight brightness based on screen content luminance.
- Configure global mouse-wheel and keyboard shortcuts.
- Keep monitor settings available even when a display is temporarily disconnected.
- Re-detect hot-plugged displays without restarting the app.
- Run from the Windows system tray with a live brightness icon.
- Optional DDC over LAN mode using TCP and mDNS discovery.
- Optional DXGI capture acceleration through `dxcam` and `numpy`.
- Optional VapourSynth input through the `BRIGHTNESS_VS_SCRIPT` environment variable.

## Requirements

- Windows.
- Python 3.10 or newer is recommended.
- Monitors that support DDC/CI for external brightness and contrast control.
- Python packages:
  - Required: `PyQt6`, `monitorcontrol`
  - Recommended or optional: `zeroconf`, `numpy`, `dxcam`, `wmi`, `vapoursynth`

## Installation

```powershell
git clone https://github.com/XPRAMT/Auto-Brightness.git
cd Auto-Brightness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PyQt6 monitorcontrol zeroconf numpy dxcam wmi
```

Install `vapoursynth` only if you plan to use a custom VapourSynth capture pipeline.

## Usage

```powershell
python .\brightness.pyw
```

The app starts as a tray application. Open the tray menu to show the main settings window, configure monitor ranges, tune automatic brightness, set shortcuts, or enable network mode.

Settings are saved in `settings.json` next to the application file. The file stores monitor ranges, global link value, shortcut configuration, automatic brightness settings, startup preference, and network mode.

## Network Mode

Network mode allows one machine to expose its local DDC/WMI monitors and another machine to control them over the LAN.

- Server mode listens on TCP port `9876` and advertises `_brightnessddc._tcp.local.` through mDNS.
- Client mode discovers servers with Zeroconf and displays remote monitors in the same UI.
- The protocol is JSON line based and supports listing monitors and setting brightness or contrast.

Enable either Server or Client from the app's Network tab. Make sure Windows Firewall allows the app or Python process to communicate on the local network.

## Automatic Brightness

Automatic brightness samples screen luminance and gradually adjusts monitor backlight toward the configured target. You can tune the target brightness, threshold, weight, capture interval, step percentage, and resource-saving idle behavior in the settings UI.

When `dxcam` and `numpy` are installed, the app can use DXGI capture. If `BRIGHTNESS_VS_SCRIPT` points to a `.vpy` file and VapourSynth is installed, that script can be used as the capture source.

## Notes

- DDC/CI must be enabled in the monitor OSD menu for most external displays.
- Some monitors or adapters do not expose brightness or contrast controls through DDC.
- WMI brightness fallback depends on Windows and display hardware support.
- If a display is missing at startup, use the refresh button to re-detect monitors after it becomes available.

---

# Auto Brightness（繁體中文）

Auto Brightness 是一個 Windows 桌面工具，以精簡的 PyQt6 系統匣應用程式控制螢幕亮度與對比。它支援 DDC/CI 外接螢幕、相容內建螢幕的 WMI 亮度備援、全域快捷鍵、依畫面內容自動調整亮度，以及區域網路遠端控制螢幕。

## 功能

- 調整支援的本機螢幕亮度與對比。
- 使用全域聯動滑桿同步多螢幕亮度。
- 根據畫面亮度自動調整背光亮度。
- 設定全域滑鼠滾輪與鍵盤快捷鍵。
- 螢幕暫時斷線時仍保留既有設定與顯示項目。
- 不需重啟即可重新偵測熱插拔螢幕。
- 以 Windows 系統匣常駐，並顯示即時亮度圖示。
- 可選用 TCP 與 mDNS 的 DDC over LAN 網路控制模式。
- 可選用 `dxcam` 與 `numpy` 啟用 DXGI 擷取加速。
- 可透過 `BRIGHTNESS_VS_SCRIPT` 環境變數指定 VapourSynth 管線。

## 需求

- Windows。
- 建議使用 Python 3.10 或更新版本。
- 外接螢幕需支援 DDC/CI 才能控制亮度與對比。
- Python 套件：
  - 必要：`PyQt6`、`monitorcontrol`
  - 建議或選用：`zeroconf`、`numpy`、`dxcam`、`wmi`、`vapoursynth`

## 安裝

```powershell
git clone https://github.com/XPRAMT/Auto-Brightness.git
cd Auto-Brightness
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PyQt6 monitorcontrol zeroconf numpy dxcam wmi
```

只有在需要自訂 VapourSynth 擷取管線時，才需要另外安裝 `vapoursynth`。

## 使用方式

```powershell
python .\brightness.pyw
```

程式會以系統匣應用程式啟動。可從系統匣選單開啟主設定視窗，調整螢幕範圍、自動亮度、快捷鍵與網路模式。

設定會儲存在程式旁的 `settings.json`。此檔案包含螢幕範圍、全域聯動亮度、快捷鍵、自動亮度設定、開機啟動偏好與網路模式。

## 網路模式

網路模式可以讓一台電腦提供本機 DDC/WMI 螢幕，另一台電腦透過區域網路遠端控制。

- Server 模式會在 TCP port `9876` 監聽，並透過 mDNS 廣播 `_brightnessddc._tcp.local.`。
- Client 模式會使用 Zeroconf 搜尋伺服器，並在同一個介面顯示遠端螢幕。
- 通訊協定採 JSON line 格式，支援列出螢幕與設定亮度或對比。

可在程式的 Network 分頁啟用 Server 或 Client。請確認 Windows 防火牆允許此程式或 Python 程序在區域網路通訊。

## 自動亮度

自動亮度會取樣畫面亮度，並逐步將螢幕背光調整到設定的目標。你可以在設定介面調整目標亮度、門檻、權重、擷取間隔、每次調整幅度與省資源閒置行為。

安裝 `dxcam` 與 `numpy` 後，程式可使用 DXGI 擷取。如果已安裝 VapourSynth，且 `BRIGHTNESS_VS_SCRIPT` 指向 `.vpy` 檔案，也可以使用該腳本作為擷取來源。

## 注意事項

- 多數外接螢幕需要先在 OSD 選單啟用 DDC/CI。
- 部分螢幕或轉接器不會透過 DDC 暴露亮度或對比控制。
- WMI 亮度備援取決於 Windows 與顯示硬體是否支援。
- 若啟動時沒有偵測到螢幕，可在螢幕恢復後按重新偵測。