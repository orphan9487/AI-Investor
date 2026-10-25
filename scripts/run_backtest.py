"""跑回測。

單段回測（明細輸出到 output/）：
  python scripts/run_backtest.py --strategy cap_weighted --start 2026-08-03 --end 2026-09-04

滾動 24 日視窗評估（比賽實際情境），可一次比較多個策略：
  python scripts/run_backtest.py --windows --strategy cap_weighted,equal_weight,momentum --start 2023-01-01
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd  # noqa: E402

from etf_agent.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from etf_agent.backtest.metrics import benchmark_return  # noqa: E402
from etf_agent.backtest.windows import describe_windows, rolling_windows  # noqa: E402
from etf_agent.data.prices import load_benchmark  # noqa: E402
from etf_agent.data.view import MarketData  # noqa: E402
from etf_agent.strategies import REGISTRY  # noqa: E402

OUT = ROOT / "output"


def _literal(v: str):
    try:
        return ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="cap_weighted", help=f"逗號分隔：{','.join(REGISTRY)}")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--exec-price", default="open", choices=["open", "close"])
    ap.add_argument("--rebalance-every", type=int, default=1)
    ap.add_argument("--band", type=float, default=0.01)
    ap.add_argument("--no-dividends", action="store_true")
    ap.add_argument("--windows", action="store_true", help="滾動視窗模式")
    ap.add_argument("--window", type=int, default=24)
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--param", action="append", default=[],
                    help="覆寫策略參數，例：--param n=20 --param tsmc_weight=0；可重複")
    ap.add_argument("--label", help="輸出檔名與表頭用的名稱（預設為策略名）")
    args = ap.parse_args()
    overrides = {k: _literal(v) for k, v in (p.split("=", 1) for p in args.param)}

    def factory(name):
        return lambda: REGISTRY[name](**overrides)

    md = MarketData.load()
    bench = load_benchmark()
    cfg = BacktestConfig(exec_price=args.exec_price, rebalance_every=args.rebalance_every,
                         band=args.band, credit_dividends=not args.no_dividends)
    names = args.strategy.split(",")
    OUT.mkdir(exist_ok=True)

    if args.windows:
        stats = {}
        for name in names:
            label = args.label or name
            print(f"{label}：滾動 {args.window} 日視窗…")
            df = rolling_windows(md, factory(name), bench, args.window, args.step, args.start, args.end, cfg)
            df.to_csv(OUT / f"windows_{label}.csv", index=False, encoding="utf-8-sig")
            stats[label] = describe_windows(df)
        table = pd.DataFrame(stats)
        pct = [r for r in table.index if r not in ("windows", "violation_windows")]
        print(table.apply(lambda col: [f"{v:.2%}" if i in pct else f"{v:.0f}" for i, v in col.items()]).to_string())
        return

    start = args.start or md.dates[-24].date().isoformat()
    end = args.end or md.dates[-1].date().isoformat()
    for name in names:
        res = run_backtest(md, factory(name)(), start, end, cfg)
        s = res.summary()
        s["bench_return"] = benchmark_return(bench, s["start"], s["end"], cfg.exec_price)
        s["excess"] = s["total_return"] - s["bench_return"]
        print(pd.Series(s).to_string())
        res.daily.to_csv(OUT / f"daily_{name}.csv", encoding="utf-8-sig")
        res.trades.to_csv(OUT / f"trades_{name}.csv", index=False, encoding="utf-8-sig")
        res.holdings.to_csv(OUT / f"holdings_{name}.csv", index=False, encoding="utf-8-sig")
        if not res.violations.empty:
            res.violations.to_csv(OUT / f"violations_{name}.csv", index=False, encoding="utf-8-sig")
            print(f"  ⚠ {s['violation_days']} 天有違規，見 output/violations_{name}.csv")
        print()


if __name__ == "__main__":
    main()
