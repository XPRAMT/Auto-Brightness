# brightness project notes

## 修改原則

- 主要程式集中在 `brightness.pyw`，修改前先用 CodeGraph 或符號搜尋確認呼叫路徑。
- 不要維護兩套螢幕偵測流程。啟動與手動重新偵測都應共用同一段初始化偵測邏輯；手動重新偵測只應額外清除既有 analyzer、DDC handle 與 UI 物件。
- `global_link_value` 代表可用螢幕 link value 的全域狀態。各螢幕 link value 可獨立變化，只有使用者主動調整全域值時才同步到所有螢幕。
- 自動亮度使用的加權亮度只供計算使用，不應寫回 `global_link_value` 或單螢幕 link value。
- WMI 螢幕不支援 contrast 時，contrast 必須維持 0，不要因自動亮度流程把 contrast 拉到最大值。
- 遠端螢幕可參與全域值計算，但遠端單螢幕 link value 不應同步改動本地螢幕 link value。

## 驗證重點

- 修改後至少執行 `python -m py_compile .\brightness.pyw`。
- 觸及快捷鍵時，要確認鍵盤 hook 與滾輪 hook 都還會啟動。
- 觸及網路同步時，要確認 server/client 模式互斥，且 debug checkbox 未啟用時不輸出網路訊號 log。
- 觸及自動亮度時，要確認每台可用本地螢幕各自擷取、計算、調整，且反應門檻仍能抑制接近目標時的跳動。

## Git

- 程式碼修改完成後自動提交 git。
- 只提交程式碼檔案；設定檔、暫存檔、執行產物不要混入程式碼提交。
