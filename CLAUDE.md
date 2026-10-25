# esun-etf-agent

AI CUP 2026 玉山人工智慧公開挑戰賽「Agent 基金經理人」。初賽 2026-10-27 ～ 11-27（24 個交易日，至少交 22 天），
複賽 12/19（簡報約占一半分數）。宣告的 ETF：「主動台股多因子成長動能」（`docs/strategy_statement.json`），
交易決策要和這個理念一致。

## 每日流程（排程：晚間 20:30、晨間 06:30，見 `docs/daily_agent_playbook.md`）

```
python scripts/agent_daily.py window      # 不在繳交窗口（前一日 19:30～當日 08:55）就結束
python scripts/agent_daily.py prepare     # 讀 inbox/ 資料包對帳、量化選股 → runs/<交易日>/context.md
# Claude 寫 runs/<交易日>/decision.json（格式見手冊步驟 6）
python scripts/agent_daily.py finalize    # → runs/<交易日>/dplan.json（使用者上傳）、report.md
python scripts/agent_daily.py compare     # 晨間：與晚間版委託比較
```

使用者每天手動：後台下載資料包放 `inbox/`、上傳 `dplan.json` 並按「送出繳交」。

## 結構

- `src/etf_agent/rules.py` 比賽規則；`data/` 資料層（yfinance＋交易所修補、FinMind、交易所每日增量）；
  `backtest/` 回測；`strategies/composite.py` 量化基準（動能／營收／法人、台積電 22%、同產業 ≤6 檔、遲滯）
- `src/etf_agent/agent/`：`pipeline.py`（prepare/finalize）、`dplan.py`（D-Plan 產生與送出前檢核）、
  `datapack.py`（平台資料包）、`decision.py`（LLM 調整範圍）、`ledger.py`、`health.py`、`report.py`
- `templates/D-Plan.schema.json` 官方 schema；`scripts/strategy_document.py` 產生策略說明文件 PDF

## 關鍵規則（違反＝當日交易作廢＋警告，3 次取消資格）

- 成交價＝交易日成交均價、全數成交；現金不可為負、不可超過 25%；持股 20–30 檔；台積電 ≤25%、其他 ≤10%
- D-Plan 委託必須等於官方推導 `floor(target_weight × nav_tradable / T-1 收盤 / 1000) × 1000`；
  引用鏈與決策覆蓋（每檔前日持股都要有 decision 或 no_trade）不完整會整份退件
- 除權息累積到賽末才入帳；股價上漲造成的權重超標有 5 個交易日緩衝

## 開發

- `python -m pytest`（約 60 個測試，2 秒）；回測：`python scripts/run_backtest.py --windows --strategy composite --start 2022-01-01`
- `--dry-run` 寫到 `dryrun/`，不影響正式的 `runs/`、`state/ledger.json`
- 資料在 `data/`（不進 git）；更新：`python scripts/update_data.py`
- 改程式後 commit，D-Plan 的 `code_version` 才是乾淨版本（12/2 要交程式碼審查）
- Windows 環境：輸出中文的指令加 `PYTHONIOENCODING=utf-8`
