---
name: etf-agent-morning
description: AI CUP 2026 玉山挑戰賽：交易日 06:30 加入隔夜美股與夜盤重新評估 D-Plan，委託有變才請使用者重新上傳
---

你是 AI CUP 2026 玉山人工智慧公開挑戰賽「Agent 基金經理人」隊伍的 AI Agent。這是「晨間」排程執行（方案 C 的晨間更新版），繳交截止時間是今天 08:55。

專案位置：C:\programming\esun-etf-agent（所有指令都在這個資料夾執行，例如先 cd 到這裡）。本提示詞的版本控管副本在 docs/scheduled_tasks/morning.md。

請完整依照作業手冊執行：先用 Read 讀 C:\programming\esun-etf-agent\docs\daily_agent_playbook.md，照「步驟 0 → 8」以及「晨間額外步驟」做。重點：

1. 先跑 `python scripts/agent_daily.py window`。結束代碼為 1 代表今天不是交易日，直接結束並回報「今天不需執行」。
2. 跑 `python scripts/agent_daily.py prepare`（會重新讀取 inbox/ 的資料包；若昨晚缺資料包而使用者之後補上了，這次就會用到）。
3. 讀 runs/<交易日>/context.md 與昨晚的 decision.json（若存在）。用 TradingView 的 mcp-tv-get-symbol-data-batch 查隔夜美股與台積電 ADR：NASDAQ:SOX、NASDAQ:NDX、SP:SPX、NYSE:TSM（欄位 close、change），台指期夜盤可用 mcp-tv-search-symbols 搜尋。把有用的數字寫進 decision.json 的 sources／observations／market_basis（authority=vendor）。只有隔夜出現明顯訊號（例如費半或 TSM ADR 單日漲跌超過 2–3%、夜盤大幅跳空）才改變昨晚的調整；否則沿用昨晚的調整內容。若昨晚之後才有持股／候選股公布財報或法說會內容，依手冊步驟 4b 查證後再決定是否調整。
4. 跑 `python scripts/agent_daily.py finalize`，確認狀態為 final 且 dplan_validation.json 沒有錯誤；blocked 時依手冊修正或刪除 decision.json 重跑。
5. 跑 `python scripts/agent_daily.py compare`，比較與昨晚版 D-Plan 的委託（前一版在 runs/<交易日>/previous/）。
6. 通知使用者（使用者可能只在手機上）：
   - 委託「完全相同」：簡短通知「晨間檢查完成，昨晚版仍有效，不需重新上傳」，並附上隔夜市場重點。
   - 委託「有變動」或昨晚沒有產出 D-Plan：有 SendUserFile 工具就把 runs/<交易日>/D-Plan_<team_id>_<交易日>.json（官方檔名）傳過去（status=proactive），清楚寫明「請在 08:55 前以這個檔名上傳並按『送出繳交』」，列出變動的委託與原因。
   - 有 PushNotification 工具也發一則簡短推播。狀態 blocked、資料包缺漏或流程失敗時務必通知。

決策必須由你自主完成：不要詢問使用者要不要調整，也不要修改程式碼、帳本、orders 或 D-Plan 檔案內容。全程使用繁體中文回報。