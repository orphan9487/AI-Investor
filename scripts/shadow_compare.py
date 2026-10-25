"""三條績效曲線（同一資產口徑：可交易淨值＝現金＋持股×收盤，不含應收配息／配股）：

  1. 純量化模擬      state/shadow_ledger.json   ← 策略基準
  2. 量化＋LLM 模擬  state/llm_sim_ledger.json  ← 與 1 用同一成交模型；2 − 1 ＝ LLM 調整的效果
  3. 平台實際        資料包 account.nav_tradable ← 3 − 2 ＝ 平台執行／結算與我們模擬的差異

  python scripts/shadow_compare.py
  python scripts/shadow_compare.py --start 2026-10-27

模擬帳本還沒有資料時，純量化改用同期回測近似（會標示出來）。
比賽只有 24 個交易日，LLM 效果的正負在統計上很難下定論，解讀時要保守。
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


def platform_nav() -> pd.Series:
    """平台實際可交易淨值：每天 context.json 記錄的資料包 account.nav_tradable（as_of_date 收盤）。"""
    rows = {}
    for ctx_path in sorted(pipeline.RUNS_DIR.glob("*/context.json")) if pipeline.RUNS_DIR.exists() else []:
        plat = json.loads(ctx_path.read_text(encoding="utf-8")).get("platform") or {}
        if plat.get("as_of_date") and plat.get("nav_tradable"):
            rows[pd.Timestamp(plat["as_of_date"])] = float(plat["nav_tradable"])
    return pd.Series(rows, dtype=float).sort_index()


def ledger_nav(path: Path) -> pd.Series:
    """模擬帳本每天記錄的收盤可交易淨值（不含累積中的除權息）。"""
    if not path.exists():
        return pd.Series(dtype=float)
    hist = json.loads(path.read_text(encoding="utf-8")).get("history", [])
    rows = {pd.Timestamp(h["date"]): h["nav"] for h in hist if h.get("event") == "close_nav"}
    return pd.Series(rows, dtype=float).sort_index()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10-27")
    args = ap.parse_args()

    md = MarketData.load()
    end = md.dates[-1]
    if pd.Timestamp(args.start) > end:
        sys.exit("比賽還沒開始累積資料")

    pure = ledger_nav(pipeline.SHADOW_LEDGER_PATH)
    if pure.empty:
        print("（純量化模擬帳本還沒有資料，暫用同期回測近似）")
        pure = run_backtest(md, CompositeScore(), args.start, end, BacktestConfig()).daily["nav"]
    curves = {"pure_quant": pure, "quant_llm_sim": ledger_nav(pipeline.LLM_SIM_LEDGER_PATH),
              "platform": platform_nav()}
    df = pd.DataFrame(curves).dropna(how="all")
    for c in curves:
        df[c + "_ret"] = df[c] / RULES.initial_capital - 1
    df["llm_effect"] = df["quant_llm_sim_ret"] - df["pure_quant_ret"]      # 同一成交模型下的 LLM 效果
    df["execution_gap"] = df["platform_ret"] - df["quant_llm_sim_ret"]    # 平台與我們模擬的差異

    out = ROOT / "output" / "shadow_compare.csv"
    out.parent.mkdir(exist_ok=True)
    df.to_csv(out, encoding="utf-8-sig")
    cols = ["pure_quant_ret", "quant_llm_sim_ret", "platform_ret", "llm_effect", "execution_gap"]
    show = df[cols].map(lambda v: "" if pd.isna(v) else f"{v:+.2%}")
    show.index = [d.date() for d in show.index]
    print(show.to_string())

    eff = df["llm_effect"].dropna()
    if not eff.empty:
        d0 = eff.index[-1]
        line = f"截至 {d0.date()}：LLM 調整效果 {eff.iloc[-1]:+.2%}（同一成交模型）"
        gap = df.at[d0, "execution_gap"]
        if pd.notna(gap):
            line += f"；平台與模擬差異 {gap:+.2%}"
        print()
        print(line)
    print("注意：比賽只有 24 天，樣本很小，LLM 效果的正負在統計上很難下定論。")
    print(f"明細：{out}")


if __name__ == "__main__":
    main()
