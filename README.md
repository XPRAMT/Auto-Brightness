# Auto Brightness

English | [繁體中文](README_TW.md)

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