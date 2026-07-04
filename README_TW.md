# Auto Brightness

[English](README.md) | 繁體中文

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

## 需求

- Windows。
- 建議使用 Python 3.10 或更新版本。
- 外接螢幕需支援 DDC/CI 才能控制亮度與對比。
- Python 套件：`PyQt6`、`monitorcontrol`、`zeroconf`、`numpy`、`dxcam`、`wmi`。

## 安裝

```powershell
git clone https://github.com/XPRAMT/Auto-Brightness.git
cd Auto-Brightness
python -m venv .venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PyQt6 monitorcontrol zeroconf numpy dxcam wmi
```

所有列出的套件都會在啟動時匯入。若缺少任一套件，程式會直接結束，方便先安裝缺少的依賴。

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

程式會使用 `dxcam` 與 `numpy` 進行 DXGI 擷取。

## 注意事項

- 多數外接螢幕需要先在 OSD 選單啟用 DDC/CI。
- 部分螢幕或轉接器不會透過 DDC 暴露亮度或對比控制。
- WMI 亮度備援取決於 Windows 與顯示硬體是否支援。
- 若啟動時沒有偵測到螢幕，可在螢幕恢復後按重新偵測。