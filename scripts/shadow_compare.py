"""影子組合比較：實際（量化＋Claude 調整，平台結算淨值）vs 純量化（同期回測）。

  python scripts/shadow_compare.py                  # 從 2026-10-27 比到最新
  python scripts/shadow_compare.py --start 2026-10-27

實際淨值取自每天 context.json 記錄的平台資料包（account.nav_tradable，as_of_date 收盤）。
純量化淨值用同一套回測引擎（成交均價、手續費與證交稅、整張）跑 CompositeScore。
兩者執行細節不完全相同（例如壓力測試保留的現金），差異約在 0.1–0.3% 等級，解讀時要考慮。
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", message="目前使用自行估算的暫定標的池")

import pandas as pd  # noqa: E402

from etf_agent.agent import pipeline  # noqa: E402
from etf_agent.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from etf_agent.data.view import MarketData  # noqa: E402
from etf_agent.rules import RULES  # noqa: E402
from etf_agent.strategies.composite import CompositeScore  # noqa: E402


def actual_nav() -> pd.Series:
    rows = {}
    for ctx_path in sorted(pipeline.RUNS_DIR.glob("*/context.json")) if pipeline.RUNS_DIR.exists() else []:
        plat = json.loads(ctx_path.read_text(encoding="utf-8")).get("platform") or {}
        if plat.get("as_of_date") and plat.get("nav_tradable"):
            rows[pd.Timestamp(plat["as_of_date"])] = float(plat["nav_tradable"])
    return pd.Series(rows, dtype=float).sort_index()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-27")
    args = ap.parse_args()

    act = actual_nav()
    md = MarketData.load()
    end = md.dates[-1]
    if pd.Timestamp(args.start) > end:
        sys.exit("比賽還沒開始累積資料")
    shadow = run_backtest(md, CompositeScore(), args.start, end, BacktestConfig()).daily["nav"]

    df = pd.DataFrame({"actual": act, "pure_quant": shadow}).dropna(how="all")
    df["actual_ret"] = df["actual"] / RULES.initial_capital - 1
    df["quant_ret"] = df["pure_quant"] / RULES.initial_capital - 1
    df["llm_value_added"] = df["actual_ret"] - df["quant_ret"]
    out = ROOT / "output" / "shadow_compare.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, encoding="utf-8-sig")
    show = df[["actual_ret", "quant_ret", "llm_value_added"]].map(lambda v: "" if pd.isna(v) else f"{v:+.2%}")
    show.index = [d.date() for d in show.index]
    print(show.to_string())
    last = df.dropna().tail(1)
    if not last.empty:
        print(f"\n截至 {last.index[0].date()}：Claude 調整的貢獻 {last['llm_value_added'].iloc[0]:+.2%}"
              f"（實際 {last['actual_ret'].iloc[0]:+.2%} vs 純量化 {last['quant_ret'].iloc[0]:+.2%}）")
    print(f"明細：{out}")


if __name__ == "__main__":
    main()
